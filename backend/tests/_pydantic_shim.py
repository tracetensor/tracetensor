"""
A tiny stand-in for pydantic v2 used ONLY for local testing in this
network-isolated sandbox. It implements just enough of BaseModel / Field /
field_validator for our schema modules to import and run.

In real deployment, the genuine pydantic package is installed (see
requirements.txt) and this shim is never imported.
"""

import copy
import sys
import types

_MISSING = object()


class _FieldInfo:
    def __init__(self, default=_MISSING, default_factory=None):
        self.default = default
        self.default_factory = default_factory

    def get_default(self):
        if self.default_factory is not None:
            return self.default_factory()
        if self.default is not _MISSING:
            return copy.deepcopy(self.default)
        return None


def Field(default=_MISSING, default_factory=None):
    return _FieldInfo(default=default, default_factory=default_factory)


def field_validator(*fields, **kwargs):
    def deco(fn):
        # `fn` may already be wrapped by @classmethod; unwrap to tag it, then
        # re-wrap so the class still sees a classmethod.
        target = fn.__func__ if isinstance(fn, classmethod) else fn
        target.__is_field_validator__ = True
        target.__validator_fields__ = fields
        return classmethod(target)

    return deco


class BaseModel:
    def __init__(self, **data):
        annotations = {}
        for klass in reversed(type(self).__mro__):
            annotations.update(getattr(klass, "__annotations__", {}))

        validators = {}
        for klass in type(self).__mro__:
            for _, attr in vars(klass).items():
                target = attr.__func__ if isinstance(attr, classmethod) else attr
                if callable(target) and getattr(target, "__is_field_validator__", False):
                    for f in target.__validator_fields__:
                        validators.setdefault(f, target)

        for field_name in annotations:
            if field_name in data:
                value = data[field_name]
            else:
                default = getattr(type(self), field_name, None)
                if isinstance(default, _FieldInfo):
                    value = default.get_default()
                else:
                    value = copy.deepcopy(default)
            if field_name in validators and value is not None:
                value = validators[field_name](type(self), value)
            setattr(self, field_name, value)

    def model_dump(self):
        out = {}
        anns = {}
        for klass in reversed(type(self).__mro__):
            anns.update(getattr(klass, "__annotations__", {}))
        for k in anns:
            v = getattr(self, k, None)
            if isinstance(v, BaseModel):
                v = v.model_dump()
            elif isinstance(v, list):
                v = [i.model_dump() if isinstance(i, BaseModel) else i for i in v]
            out[k] = v
        return out


def install():
    mod = types.ModuleType("pydantic")
    mod.BaseModel = BaseModel
    mod.Field = Field
    mod.field_validator = field_validator
    sys.modules["pydantic"] = mod
