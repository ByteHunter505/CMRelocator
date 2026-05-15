"""Async CMIS 1.1 Browser Binding client.

Minimal client targeted at IBM Content Manager v8 but compatible with any
CMIS-compliant repository exposing the Browser Binding (JSON).

Only the operations needed by CMRelocator are implemented:
- fetch_repositories     -> service document
- get_folder             -> object properties of a folder
- get_object_parents     -> parent folders of any object
- get_type_definition    -> full type definition (id, queryName, properties)
- list_documents_in_folder
- list_folders_by_type   -> folders of a custom ItemType filtered by CIF
- list_children          -> direct children (folders + docs) of a folder
- create_folder          -> CMIS createFolder
- move_object            -> CMIS moveObject (cmisaction=move)
- delete_object          -> CMIS deleteObject (cmisaction=delete)

CMIS SQL note: The OASIS CMIS 1.1 grammar does not allow quoted identifiers.
Type and property references in queries must be the `queryName`, not the `id`.
This client resolves queryNames automatically via getTypeDefinition.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class CmisError(Exception):
    """Raised when a CMIS operation fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class RepositoryInfo:
    repository_id: str
    repository_url: str
    root_folder_url: str
    name: str
    product_name: str
    product_version: str


@dataclass
class CmisDocument:
    object_id: str
    name: str
    content_stream_length: int | None
    content_stream_mime_type: str | None
    last_modified: str | None
    object_type_id: str | None


@dataclass
class CmisFolder:
    object_id: str
    name: str
    cif: str
    object_type_id: str


@dataclass
class CmisChild:
    """A direct child of a folder (either a sub-folder or a document)."""
    object_id: str
    name: str
    is_folder: bool
    content_stream_length: int | None
    content_stream_mime_type: str | None
    last_modified: str | None
    object_type_id: str | None


class CmisClient:
    """Async CMIS 1.1 Browser Binding client."""

    def __init__(
        self,
        service_url: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 60.0,
    ) -> None:
        self._service_url = service_url.rstrip("/")
        self._client = httpx.AsyncClient(
            auth=(username, password),
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
        )
        self._repositories: dict[str, RepositoryInfo] = {}

    async def __aenter__(self) -> "CmisClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_repositories(self) -> dict[str, RepositoryInfo]:
        resp = await self._client.get(self._service_url)
        self._raise_for_status(resp)
        data = resp.json()
        repositories: dict[str, RepositoryInfo] = {}
        for repo_id, info in data.items():
            repositories[repo_id] = RepositoryInfo(
                repository_id=repo_id,
                repository_url=info["repositoryUrl"],
                root_folder_url=info["rootFolderUrl"],
                name=info.get("repositoryName", repo_id),
                product_name=info.get("productName", ""),
                product_version=info.get("productVersion", ""),
            )
        self._repositories = repositories
        return repositories

    def repository(self, repository_id: str) -> RepositoryInfo:
        try:
            return self._repositories[repository_id]
        except KeyError as exc:
            raise CmisError(
                f"Repository {repository_id!r} not loaded; call fetch_repositories() first."
            ) from exc

    async def get_folder(self, repository_id: str, folder_id: str) -> dict[str, Any]:
        repo = self.repository(repository_id)
        params = {"cmisselector": "object", "objectId": folder_id, "succinct": "true"}
        resp = await self._client.get(repo.root_folder_url, params=params)
        self._raise_for_status(resp)
        return resp.json()

    async def get_object_parents(
        self, repository_id: str, object_id: str
    ) -> list[str]:
        """Return objectIds of the parent folders of an object.

        Folders are typically single-parented; documents may be multi-filed.
        """
        repo = self.repository(repository_id)
        params = {"cmisselector": "parents", "objectId": object_id, "succinct": "true"}
        resp = await self._client.get(repo.root_folder_url, params=params)
        self._raise_for_status(resp)
        payload = resp.json()
        entries = payload if isinstance(payload, list) else payload.get("objects", []) or []
        parents: list[str] = []
        for entry in entries:
            obj = entry.get("object", entry) if isinstance(entry, dict) else entry
            if not isinstance(obj, dict):
                continue
            props = obj.get("properties", {}) or obj.get("succinctProperties", {})
            pid = _prop(props, "cmis:objectId")
            if pid:
                parents.append(str(pid))
        return parents

    async def get_type_definition(
        self, repository_id: str, type_id: str
    ) -> dict[str, Any]:
        """Fetch a full type definition (includes queryName + property defs)."""
        repo = self.repository(repository_id)
        params = {"cmisselector": "typeDefinition", "typeId": type_id}
        resp = await self._client.get(repo.repository_url, params=params)
        self._raise_for_status(resp)
        return resp.json()

    async def resolve_query_names(
        self,
        repository_id: str,
        type_id: str,
        property_id: str,
    ) -> tuple[str, str]:
        """Return (type_queryName, property_queryName) for use in CMIS SQL."""
        typedef = await self.get_type_definition(repository_id, type_id)
        type_qn = typedef.get("queryName") or typedef.get("query_name")
        if not type_qn:
            raise CmisError(f"Type {type_id!r} has no queryName in its definition.")
        prop_defs = typedef.get("propertyDefinitions") or {}
        pdef = prop_defs.get(property_id)
        if pdef is None:
            for candidate in prop_defs.values():
                if isinstance(candidate, dict) and candidate.get("id") == property_id:
                    pdef = candidate
                    break
        if pdef is None:
            available = list(prop_defs.keys())[:20]
            raise CmisError(
                f"Property {property_id!r} not defined on type {type_id!r}. "
                f"Sample available property ids: {available}"
            )
        prop_qn = pdef.get("queryName") or pdef.get("query_name") or pdef.get("localName")
        if not prop_qn:
            raise CmisError(
                f"Property {property_id!r} on type {type_id!r} has no queryName."
            )
        return type_qn, prop_qn

    async def list_documents_in_folder(
        self,
        repository_id: str,
        folder_id: str,
        *,
        max_items: int = 1000,
    ) -> list[CmisDocument]:
        """List documents directly contained in `folder_id` via CMIS query."""
        repo = self.repository(repository_id)
        statement = (
            "SELECT cmis:objectId, cmis:name, cmis:contentStreamLength, "
            "cmis:contentStreamMimeType, cmis:lastModificationDate, cmis:objectTypeId "
            "FROM cmis:document "
            f"WHERE IN_FOLDER({_q_literal(folder_id)})"
        )
        data = {
            "cmisaction": "query",
            "statement": statement,
            "searchAllVersions": "false",
            "maxItems": str(max_items),
            "skipCount": "0",
        }
        resp = await self._client.post(repo.repository_url, data=data)
        self._raise_for_status(resp)
        payload = resp.json()
        results: list[CmisDocument] = []
        for row in payload.get("results", []):
            props = row.get("properties", {}) or row.get("succinctProperties", {})
            results.append(
                CmisDocument(
                    object_id=_prop(props, "cmis:objectId") or "",
                    name=_prop(props, "cmis:name") or "",
                    content_stream_length=_to_int(_prop(props, "cmis:contentStreamLength")),
                    content_stream_mime_type=_prop(props, "cmis:contentStreamMimeType"),
                    last_modified=_prop(props, "cmis:lastModificationDate"),
                    object_type_id=_prop(props, "cmis:objectTypeId"),
                )
            )
        return results

    async def list_folders_by_type(
        self,
        repository_id: str,
        type_id: str,
        *,
        cif: str | None = None,
        cif_property: str = "clbNonGroup.BAC_CIF",
        max_items: int = 50_000,
        page_size: int = 500,
    ) -> tuple[list[CmisFolder], bool]:
        """Query folders of a custom ItemType, paginated via skipCount.

        Resolves queryName for both the type and the CIF property (the CMIS
        SQL grammar does not allow quoted identifiers).

        Pages through `skipCount` in batches of `page_size` until the server
        reports no more items, or `max_items` is reached.

        Returns (folders, hit_cap) where `hit_cap` is True iff we stopped
        because we reached `max_items` (vs. because the server said there
        were no more results). The caller can use that to warn the user
        their `max_items` may be too tight.
        """
        repo = self.repository(repository_id)
        type_qn, cif_qn = await self.resolve_query_names(
            repository_id, type_id, cif_property
        )
        statement_base = (
            f"SELECT cmis:objectId, cmis:name, cmis:objectTypeId, {cif_qn} "
            f"FROM {type_qn}"
        )
        if cif:
            statement_base += f" WHERE {cif_qn} = {_q_literal(cif)}"

        folders: list[CmisFolder] = []
        skip = 0
        hit_cap = False
        while len(folders) < max_items:
            batch = min(page_size, max_items - len(folders))
            data = {
                "cmisaction": "query",
                "statement": statement_base,
                "searchAllVersions": "false",
                "maxItems": str(batch),
                "skipCount": str(skip),
            }
            resp = await self._client.post(repo.repository_url, data=data)
            self._raise_for_status(resp)
            payload = resp.json()
            results = payload.get("results", []) or []
            if not results:
                break
            # Defensive: some servers ignore maxItems and return more rows
            # than requested. Clip to the remaining budget so we honour
            # max_items strictly.
            remaining = max_items - len(folders)
            results = results[:remaining]
            for row in results:
                props = row.get("properties", {}) or row.get("succinctProperties", {})
                cif_val = _prop(props, cif_qn)
                if cif_val is None:
                    cif_val = _prop(props, cif_property)
                folders.append(
                    CmisFolder(
                        object_id=_prop(props, "cmis:objectId") or "",
                        name=_prop(props, "cmis:name") or "",
                        cif="" if cif_val is None else str(cif_val),
                        object_type_id=_prop(props, "cmis:objectTypeId") or type_id,
                    )
                )
            skip += len(results)
            if not payload.get("hasMoreItems", False):
                break
        else:
            # Loop exited via `while` condition (not `break`) -> we hit the cap.
            hit_cap = True
        return folders, hit_cap

    async def list_children(
        self,
        repository_id: str,
        folder_id: str,
        *,
        max_items: int = 1000,
    ) -> list[CmisChild]:
        """List direct children (sub-folders + documents) of a folder.

        Uses the CMIS Browser Binding `children` selector, which returns all
        child objects regardless of base type. This is what the migration
        flow uses: each child is moved into the target folder via
        moveObject, and CMIS atomically re-parents folders along with their
        entire subtree.
        """
        repo = self.repository(repository_id)
        params = {
            "cmisselector": "children",
            "objectId": folder_id,
            "maxItems": str(max_items),
            "skipCount": "0",
        }
        resp = await self._client.get(repo.root_folder_url, params=params)
        self._raise_for_status(resp)
        payload = resp.json()
        results: list[CmisChild] = []
        for entry in payload.get("objects", []):
            obj = entry.get("object", entry)
            props = obj.get("properties", {}) or obj.get("succinctProperties", {})
            base_type = _prop(props, "cmis:baseTypeId") or ""
            results.append(
                CmisChild(
                    object_id=_prop(props, "cmis:objectId") or "",
                    name=_prop(props, "cmis:name") or "",
                    is_folder=(base_type == "cmis:folder"),
                    content_stream_length=_to_int(_prop(props, "cmis:contentStreamLength")),
                    content_stream_mime_type=_prop(props, "cmis:contentStreamMimeType"),
                    last_modified=_prop(props, "cmis:lastModificationDate"),
                    object_type_id=_prop(props, "cmis:objectTypeId"),
                )
            )
        return results

    async def move_object(
        self,
        repository_id: str,
        object_id: str,
        source_folder_id: str,
        target_folder_id: str,
    ) -> dict[str, Any]:
        """CMIS moveObject. Returns updated object JSON, or {} on 204."""
        repo = self.repository(repository_id)
        data = {
            "cmisaction": "move",
            "objectId": object_id,
            "sourceFolderId": source_folder_id,
            "targetFolderId": target_folder_id,
        }
        resp = await self._client.post(repo.root_folder_url, data=data)
        self._raise_for_status(resp)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    async def create_folder(
        self,
        repository_id: str,
        parent_folder_id: str,
        name: str,
        type_id: str,
        *,
        custom_properties: dict[str, Any] | None = None,
    ) -> str:
        """Create a folder under `parent_folder_id` and return its new objectId.

        Sets cmis:name, cmis:objectTypeId, plus any custom_properties (mapping
        of property id -> value). If the type has additional required
        properties not provided here, the server will reject the request.
        """
        repo = self.repository(repository_id)
        data: dict[str, Any] = {
            "cmisaction": "createFolder",
            "objectId": parent_folder_id,
            "propertyId[0]": "cmis:name",
            "propertyValue[0]": name,
            "propertyId[1]": "cmis:objectTypeId",
            "propertyValue[1]": type_id,
        }
        idx = 2
        for key, value in (custom_properties or {}).items():
            data[f"propertyId[{idx}]"] = key
            data[f"propertyValue[{idx}]"] = "" if value is None else str(value)
            idx += 1
        resp = await self._client.post(repo.root_folder_url, data=data)
        self._raise_for_status(resp)
        if not resp.content:
            raise CmisError("createFolder returned an empty body; no objectId.")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CmisError(f"createFolder returned non-JSON body: {exc}") from exc
        props = payload.get("properties", {}) or payload.get("succinctProperties", {})
        new_id = _prop(props, "cmis:objectId")
        if not new_id:
            raise CmisError("createFolder succeeded but no cmis:objectId in response.")
        return str(new_id)

    async def delete_object(
        self, repository_id: str, object_id: str
    ) -> None:
        """CMIS deleteObject. For folders the server will refuse if it is
        non-empty (no recursive descent). Use this only on folders you have
        already confirmed are empty.
        """
        repo = self.repository(repository_id)
        data = {
            "cmisaction": "delete",
            "objectId": object_id,
        }
        resp = await self._client.post(repo.root_folder_url, data=data)
        self._raise_for_status(resp)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        body = resp.text
        message: str = body
        try:
            payload = resp.json()
            message = (
                payload.get("message")
                or payload.get("exception")
                or payload.get("error")
                or body
            )
        except ValueError:
            pass
        raise CmisError(
            f"HTTP {resp.status_code}: {message}",
            status_code=resp.status_code,
            body=body,
        )


def _prop(props: dict[str, Any], key: str) -> Any:
    """Extract a property value from either full or succinct properties map."""
    raw = props.get(key)
    if raw is None:
        return None
    if isinstance(raw, dict):
        value = raw.get("value")
        if isinstance(value, list):
            return value[0] if value else None
        return value
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _q_literal(value: str) -> str:
    """Single-quote a CMIS SQL string literal (escapes embedded single quotes)."""
    return "'" + value.replace("'", "''") + "'"
