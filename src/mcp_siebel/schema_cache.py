"""Small in-memory schema cache for Siebel Business Object metadata."""

from __future__ import annotations

from .siebel_client import SiebelBCDescriptor, SiebelClient


class SchemaCache:
    def __init__(self, client: SiebelClient) -> None:
        self.client = client
        self._business_objects: list[str] | None = None
        self._descriptors: dict[tuple[str, str], SiebelBCDescriptor] = {}

    async def get_business_objects(self) -> list[str]:
        if self._business_objects is None:
            self._business_objects = await self.client.list_business_objects()
        return self._business_objects

    async def resolve_business_object(self, name: str) -> str | None:
        objects = await self.get_business_objects()
        by_lower = {item.lower(): item for item in objects}
        return by_lower.get(name.lower())

    async def get_descriptor(self, business_object: str, business_component: str) -> SiebelBCDescriptor:
        key = (business_object, business_component)
        if key not in self._descriptors:
            self._descriptors[key] = await self.client.describe_bc(business_object, business_component)
        return self._descriptors[key]

    @staticmethod
    def field_summary(descriptor: SiebelBCDescriptor) -> str:
        lines: list[str] = []
        for field in descriptor.fields:
            flags: list[str] = []
            if field.required:
                flags.append("required")
            if field.read_only:
                flags.append("read-only")
            if field.max_length:
                flags.append(f"max {field.max_length}")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"  - {field.name}: {field.type}{suffix}")
        return "\n".join(lines)
