# -*- coding: utf-8 -*-
#
# Copyright 2017-2020- Swiss Data Science Center (SDSC)
# A partnership between École Polytechnique Fédérale de Lausanne (EPFL) and
# Eidgenössische Technische Hochschule Zürich (ETHZ).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Marshmallow schema implementation that supports Json-LD."""

from functools import lru_cache
from marshmallow.schema import Schema, SchemaOpts
from marshmallow.utils import missing, is_collection, RAISE, set_value, EXCLUDE, INCLUDE
from marshmallow import post_load
from collections.abc import Mapping
from marshmallow.error_store import ErrorStore

from pyld import jsonld

import inspect

import typing

from calamus.utils import normalize_id, normalize_type

_T = typing.TypeVar("_T")


class JsonLDSchemaOpts(SchemaOpts):
    """Options class for `JsonLDSchema`.
    Adds the following options:
    - ``rdf_type``: The RDF type(s) for this schema.
    - ``model``: The python type this schema (de-)serializes.
    - ``add_value_types``: Whether to add ``@type`` information to scalar field values.
    """

    def __init__(self, meta, *args, **kwargs):
        super().__init__(meta, *args, **kwargs)

        self.rdf_type = getattr(meta, "rdf_type", None)
        if not isinstance(self.rdf_type, list):
            self.rdf_type = [self.rdf_type]
        self.rdf_type = sorted(self.rdf_type)

        self.model = getattr(meta, "model", None)
        self.add_value_types = getattr(meta, "add_value_types", False)


class JsonLDSchema(Schema):
    """Schema for a JsonLD class.
    Example: ::
        from calamus import JsonLDSchema
        import calamus.fields as fields
        from mymodels import User
        schema = fields.Namespace("http://schema.org/")
        class UserSchema(JsonLDSchema):
            class Meta:
                rdf_type = schema.Person
                model = User
            _id = fields.Id()
            birth_date = fields.Date(schema.birthDate)
            name = fields.String(schema.name)
    """

    OPTIONS_CLASS = JsonLDSchemaOpts

    def __init__(
        self,
        *args,
        only=None,
        exclude=(),
        many=False,
        context=None,
        load_only=(),
        dump_only=(),
        partial=False,
        unknown=None,
        flattened=False,
        _all_objects=None,
    ):
        super().__init__(
            *args,
            only=only,
            exclude=exclude,
            many=many,
            context=context,
            load_only=load_only,
            dump_only=dump_only,
            partial=partial,
            unknown=unknown,
        )

        self.flattened = flattened
        self._all_objects = _all_objects

        if not self.opts.rdf_type or not self.opts.model:
            raise ValueError("rdf_type and model have to be set on the Meta of schema {}".format(type(self)))

    def _serialize(self, obj: typing.Union[_T, typing.Iterable[_T]], *, many: bool = False):
        """Serialize ``obj`` to jsonld."""
        if many and obj is not None:
            return [self._serialize(d, many=False) for d in typing.cast(typing.Iterable[_T], obj)]
        ret = self.dict_class()
        for attr_name, field_obj in self.dump_fields.items():
            value = field_obj.serialize(attr_name, obj, accessor=self.get_attribute)
            if value is missing:
                continue
            key = field_obj.data_key if field_obj.data_key is not None else attr_name
            reverse = getattr(field_obj, "reverse", False)
            if reverse:
                if "@reverse" not in ret:
                    ret["@reverse"] = self.dict_class()
                ret["@reverse"][key] = value
            else:
                ret[key] = value

        # add type
        rdf_type = self.opts.rdf_type

        if not rdf_type:
            raise ValueError("No class type specified for schema")

        ret["@type"] = normalize_type(rdf_type)

        if self.flattened:
            ret = jsonld.flatten(ret)

        return ret

    def get_reverse_links(self, data: typing.Mapping[str, typing.Any], field_name: str):
        """Get all objects pointing to the object in data with the field fieldname.

        Used for unflattening a list.
        """
        ret = []

        for d in self._all_objects.values():
            if field_name not in d:
                continue

            if self._compare_ids(d[field_name], data["@id"]):
                ret.append(d["@id"])

        return ret

    def _compare_ids(self, first, second):
        """Compare if two ids or lists of ids match."""

        first = set(normalize_id(first))
        second = set(normalize_id(second))

        return first & second == first | second

    def _deserialize(
        self,
        data: typing.Union[typing.Mapping[str, typing.Any], typing.Iterable[typing.Mapping[str, typing.Any]],],
        *args,
        error_store: ErrorStore,
        many: bool = False,
        partial=False,
        unknown=RAISE,
        index=None,
    ) -> typing.Union[_T, typing.List[_T]]:
        index_errors = self.opts.index_errors
        index = index if index_errors else None

        if self.flattened and is_collection(data) and not self._all_objects:
            new_data = []
            self._all_objects = {}

            for d in data:
                self._all_objects[d["@id"]] = d

                if self._compare_ids(d["@type"], self.opts.rdf_type):
                    new_data.append(d)
            data = new_data

            if len(data) == 1:
                data = data[0]

        if many:
            if not is_collection(data):
                error_store.store_error([self.error_messages["type"]], index=index)
                ret = []  # type: typing.List[_T]
            else:
                ret = [
                    typing.cast(
                        _T,
                        self._deserialize(
                            typing.cast(typing.Mapping[str, typing.Any], d),
                            error_store=error_store,
                            many=False,
                            partial=partial,
                            unknown=unknown,
                            index=idx,
                        ),
                    )
                    for idx, d in enumerate(data)
                ]
            return ret
        ret = self.dict_class()
        # Check data is a dict
        if not isinstance(data, Mapping):
            error_store.store_error([self.error_messages["type"]], index=index)
        else:
            if data.get("@context", None):
                # we got compacted jsonld, expand it
                data = jsonld.expand(data)

            partial_is_collection = is_collection(partial)
            for attr_name, field_obj in self.load_fields.items():
                field_name = field_obj.data_key if field_obj.data_key is not None else attr_name

                if getattr(field_obj, "reverse", False):
                    raw_value = data.get("@reverse", missing)
                    if raw_value is not missing:
                        raw_value = raw_value.get(field_name, missing)
                    elif self.flattened:
                        # find an object that refers to this one with the same property
                        raw_value = self.get_reverse_links(data, field_name)

                        if not raw_value:
                            raw_value = missing
                else:
                    raw_value = data.get(field_name, missing)

                if raw_value is missing:
                    # Ignore missing field if we're allowed to.
                    if partial is True or (partial_is_collection and attr_name in partial):
                        continue

                d_kwargs = {}
                # Allow partial loading of nested schemas.
                if partial_is_collection:
                    prefix = field_name + "."
                    len_prefix = len(prefix)
                    sub_partial = [f[len_prefix:] for f in partial if f.startswith(prefix)]
                    d_kwargs["partial"] = sub_partial
                else:
                    d_kwargs["partial"] = partial

                d_kwargs["_all_objects"] = self._all_objects
                d_kwargs["flattened"] = self.flattened
                getter = lambda val: field_obj.deserialize(val, field_name, data, **d_kwargs)
                value = self._call_and_store(
                    getter_func=getter, data=raw_value, field_name=field_name, error_store=error_store, index=index,
                )
                if value is not missing:
                    key = field_obj.attribute or attr_name
                    set_value(typing.cast(typing.Dict, ret), key, value)
            if unknown != EXCLUDE:
                fields = {
                    field_obj.data_key if field_obj.data_key is not None else field_name
                    for field_name, field_obj in self.load_fields.items()
                }
                for key in set(data) - fields:
                    if key in ["@type", "@reverse"]:
                        # ignore JsonLD meta fields
                        continue

                    value = data[key]
                    if unknown == INCLUDE:
                        set_value(typing.cast(typing.Dict, ret), key, value)
                    elif unknown == RAISE:
                        error_store.store_error(
                            [self.error_messages["unknown"]], key, (index if index_errors else None),
                        )

        return ret

    @post_load
    def make_instance(self, data, **kwargs):
        """Transform loaded dict into corresponding object."""

        const_args = inspect.signature(self.opts.model)
        keys = set(data.keys())
        args = []
        kwargs = {}
        has_kwargs = False
        for _, parameter in const_args.parameters.items():
            if parameter.kind in [inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY]:
                if parameter.name not in keys:
                    raise ValueError("Field {} not found in data {}".format(parameter.name, data))
                args.append(data[parameter.name])
                keys.remove(parameter.name)
            elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                if parameter.name in keys:
                    kwargs[parameter.name] = data[parameter.name]
                    keys.remove(parameter.name)
            elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_kwargs = True
        missing_data = {k: v for k, v in data.items() if k in keys}
        if has_kwargs:
            instance = self.opts.model(*args, **kwargs, **missing_data)
        else:
            instance = self.opts.model(*args, **kwargs)

        unset_data = {}
        for key, value in missing_data.items():
            if hasattr(instance, key) and not getattr(instance, key):
                setattr(instance, key, value)
            else:
                unset_data[key] = value

        if unset_data:
            raise ValueError(
                "The following fields were not found on class {}:\n\t{}".format(
                    self.opts.model, "\n\t".join(unset_data.keys())
                )
            )

        return instance


class JsonLDAnnotation(type):
    """Metaclass allowing automated generation of calamus schema based on annotations.

    Example:

    .. code-block:: python

       import datetime.datetime as dt

       from calamus import JsonLDAnnotation
       import calamus.fields as fields

       schema = fields.Namespace("http://schema.org/")

       class User(metaclass=JsonLDAnnotation):
           class Meta:
               rdf_type = schema.Person
           _id = fields.Id()
           birth_date = fields.Date(schema.birthDate, default=dt.now)
           name = fields.String(schema.name, default=lambda: "John")

        user = User()

        # dumping
        User.schema().dump(user)
        # or
        user.dump()

        # loading
        u = User.schema().load({"_id": "http://example.com/user/1", "name": "Bill", "birth_date": "1970-01-01 00:00"})
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        import calamus.fields as fields

        attribute_dict = {}
        for attr_name, value in namespace.copy().items():
            if isinstance(value, fields._JsonLDField) or isinstance(value, fields.Id):
                attribute_dict[attr_name] = value

                if hasattr(value, "default"):
                    if callable(value.default):
                        namespace[attr_name] = value.default()
                    else:
                        namespace[attr_name] = value.default
                else:
                    del namespace[attr_name]

        if "Meta" not in namespace or not hasattr(namespace["Meta"], "rdf_type"):
            raise ValueError("Setting 'rdf_type' on the `class Meta` is required for calamus annotations")

        attribute_dict["Meta"] = type("Meta", (), {"rdf_type": namespace["Meta"].rdf_type})
        namespace["__calamus_schema__"] = type(f"{name}Schema", (JsonLDSchema,), attribute_dict)

        @lru_cache(maxsize=5)
        def schema(*args, **kwargs):
            """Convenience method to access calamus schema of a class."""
            return namespace["__calamus_schema__"](*args, **kwargs)

        namespace[schema.__name__] = schema

        def dump(self, *args, **kwargs):
            """Convenience method to dump object directly."""
            return type(self).schema(*args, **kwargs).dump(self)

        namespace[dump.__name__] = dump

        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        namespace["__calamus_schema__"].Meta.model = cls
        namespace["__calamus_schema__"].opts.model = cls

        return cls
