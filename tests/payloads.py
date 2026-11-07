"""Изменяемые недоверенные данные для отрицательных wire-проверок."""
from collections.abc import Callable
from pydantic import TypeAdapter

OBJECT = TypeAdapter(dict[str, object])


class Payload(dict[str, object]):
    @classmethod
    def validated(cls, value: object) -> 'Payload':
        return cls(OBJECT.validate_python(value, strict=True))

    def child(self, key: str) -> 'Payload':
        value = self[key]
        if isinstance(value, Payload):
            return value
        child = Payload(OBJECT.validate_python(value, strict=True))
        self[key] = child
        return child


def mutations(values: list[Callable[[Payload], object]]) -> list[Callable[[Payload], object]]:
    return values
