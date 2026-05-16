"""Async CMIS 1.1 Browser Binding client.

Minimal client targeted at IBM Content Manager v8 but compatible with any
CMIS-compliant repository exposing the Browser Binding (JSON).

Only the operations needed by CMRelocator are implemented:
- fetch_repositories          -> service document
- get_folder                  -> object properties of a folder
- get_object_parents          -> parent folders of any object
- get_type_definition         -> full type definition (id, queryName, properties)
- list_documents_in_folder
- list_documents_of_type_in_folder -> docs of a specific type in a folder
- list_folders_by_type        -> folders of a custom ItemType filtered by CIF
- list_children               -> direct children (folders + docs) of a folder
- search_by_property          -> LIKE substring search on a custom property of a type
- create_folder               -> CMIS createFolder
- move_object                 -> CMIS moveObject (cmisaction=move)
- delete_object               -> CMIS deleteObject (cmisaction=delete)

CMIS SQL note: The OASIS CMIS 1.1 grammar does not allow quoted identifiers.
Type and property references in queries must be the `queryName`, not the `id`.
This client resolves queryNames automatically via getTypeDefinition.
"""
from __future__ import annotations

import asyncio
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


@dataclass
class CmisSearchResult:
    """A property-search hit on a single custom ItemType."""
    object_id: str
    name: str
    object_type_id: str
    property_value: str
    is_folder: bool = True


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
        # repo_id -> root_type_id -> flat list of typedef dicts.
        # Populated lazily by get_type_descendants; type trees don't
        # change during a session so a single fetch is plenty.
        self._descendants_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

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

    async def get_type_descendants(
        self,
        repository_id: str,
        *,
        root_type_id: str = "cmis:folder",
        depth: int = -1,
        include_property_definitions: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch and flatten the type tree rooted at `root_type_id`.

        Returns each descendant as its full type-definition dict (including
        `parentTypeId` and, when requested, `propertyDefinitions`). Does
        NOT include the root itself in the result.

        Cached per (repo, root_type_id) -- the type tree is fixed for the
        lifetime of the session.
        """
        cache = self._descendants_cache.setdefault(repository_id, {})
        if root_type_id in cache:
            return cache[root_type_id]

        repo = self.repository(repository_id)
        params: dict[str, Any] = {
            "cmisselector": "typeDescendants",
            "typeId": root_type_id,
            "depth": str(depth),
            "includePropertyDefinitions": (
                "true" if include_property_definitions else "false"
            ),
        }
        resp = await self._client.get(repo.repository_url, params=params)
        self._raise_for_status(resp)
        tree = resp.json()

        out: list[dict[str, Any]] = []

        def walk(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                t = n.get("type") or {}
                if isinstance(t, dict) and t.get("id"):
                    out.append(t)
                walk(n.get("children"))

        walk(tree)
        cache[root_type_id] = out
        return out

    async def find_property_owning_types(
        self,
        repository_id: str,
        property_id: str,
        *,
        root_type_id: str = "cmis:folder",
    ) -> list[str]:
        """Return the "frontier" of types under `root_type_id` that define
        `property_id` -- i.e. the topmost types in the hierarchy where
        the property appears.

        CMIS SQL `SELECT FROM T` includes instances of T and all of T's
        subtypes by default, so querying each frontier type is sufficient
        to cover everything in the tree that has the property; no
        deduplication is required because an object belongs to exactly
        one concrete type.

        A type T is in the frontier iff:
          1. T's propertyDefinitions contain `property_id`, AND
          2. T's parent type is either outside the walked tree (e.g.
             cmis:folder itself), or does NOT have the property in
             its propertyDefinitions.
        """
        descendants = await self.get_type_descendants(
            repository_id,
            root_type_id=root_type_id,
            depth=-1,
            include_property_definitions=True,
        )
        by_id: dict[str, dict[str, Any]] = {}
        for t in descendants:
            tid = t.get("id")
            if isinstance(tid, str):
                by_id[tid] = t

        def has_prop(t: dict[str, Any] | None) -> bool:
            if t is None:
                return False
            props = t.get("propertyDefinitions") or {}
            if not isinstance(props, dict):
                return False
            if property_id in props:
                return True
            for pdef in props.values():
                if isinstance(pdef, dict) and pdef.get("id") == property_id:
                    return True
            return False

        frontier: list[str] = []
        for tid, tdef in by_id.items():
            if not has_prop(tdef):
                continue
            parent_id = tdef.get("parentTypeId") or tdef.get("parentId")
            parent_def = by_id.get(parent_id) if isinstance(parent_id, str) else None
            if not has_prop(parent_def):
                frontier.append(tid)
        return frontier

    async def find_types_by_description(
        self,
        repository_id: str,
        pattern: str,
        *,
        root_type_id: str = "cmis:folder",
        case_insensitive: bool = True,
    ) -> list[str]:
        """Return type ids whose `description` (type-definition metadata,
        NOT the cmis:description property of an instance) contains `pattern`
        as a substring.

        Matches client-side over the cached type tree -- the CMIS query
        grammar cannot filter on type-definition metadata, only on
        queryable properties of instances.
        """
        if not pattern:
            return []
        descendants = await self.get_type_descendants(
            repository_id, root_type_id=root_type_id
        )
        needle = pattern.casefold() if case_insensitive else pattern
        out: list[str] = []
        for t in descendants:
            desc = t.get("description") or ""
            if not isinstance(desc, str):
                continue
            hay = desc.casefold() if case_insensitive else desc
            if needle in hay:
                tid = t.get("id")
                if isinstance(tid, str):
                    out.append(tid)
        return out

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

        Uses the CMIS Browser Binding `children` selector. Tolerant of
        response-shape variations across CMIS implementations:
        - The spec wraps results as `{"objects": [{"object": <data>}]}`
          but some servers return a bare list or omit the inner "object".
        - The list key is usually "objects"; some servers use "items".
        - Each row may carry properties under "properties" (full mode) or
          "succinctProperties" (succinct mode).

        Returns CmisChild for each direct child regardless of base type.
        """
        repo = self.repository(repository_id)
        params = {
            "cmisselector": "children",
            "objectId": folder_id,
            "maxItems": str(max_items),
            "skipCount": "0",
            "includeAllowableActions": "false",
        }
        resp = await self._client.get(repo.root_folder_url, params=params)
        self._raise_for_status(resp)
        payload = resp.json()

        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = payload.get("objects") or payload.get("items") or []
        else:
            entries = []

        results: list[CmisChild] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("object")
            obj = inner if isinstance(inner, dict) else entry
            props = obj.get("properties") or obj.get("succinctProperties") or {}
            if not isinstance(props, dict):
                continue
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

    async def list_documents_of_type_in_folder(
        self,
        repository_id: str,
        folder_id: str,
        doc_type_id: str,
        *,
        max_items: int = 1000,
    ) -> list[CmisChild]:
        """Documents of a specific custom type that live directly in a folder.

        Used by the TUI's "File" migration mode: instead of listing all
        children of a source folder (which folder mode does), restrict to
        documents whose `cmis:objectTypeId` matches `doc_type_id`. Useful
        when a customer folder has many document kinds and you want to
        move only one kind at a time.
        """
        repo = self.repository(repository_id)
        typedef = await self.get_type_definition(repository_id, doc_type_id)
        type_qn = typedef.get("queryName") or typedef.get("query_name")
        if not type_qn:
            raise CmisError(
                f"Document type {doc_type_id!r} has no queryName."
            )
        statement = (
            "SELECT cmis:objectId, cmis:name, cmis:contentStreamLength, "
            "cmis:contentStreamMimeType, cmis:lastModificationDate, "
            "cmis:objectTypeId "
            f"FROM {type_qn} "
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
        results: list[CmisChild] = []
        for row in payload.get("results", []):
            props = row.get("properties", {}) or row.get("succinctProperties", {})
            results.append(
                CmisChild(
                    object_id=_prop(props, "cmis:objectId") or "",
                    name=_prop(props, "cmis:name") or "",
                    is_folder=False,
                    content_stream_length=_to_int(_prop(props, "cmis:contentStreamLength")),
                    content_stream_mime_type=_prop(props, "cmis:contentStreamMimeType"),
                    last_modified=_prop(props, "cmis:lastModificationDate"),
                    object_type_id=_prop(props, "cmis:objectTypeId"),
                )
            )
        return results

    async def list_documents_of_type_by_cif(
        self,
        repository_id: str,
        doc_type_id: str,
        *,
        cif: str | None = None,
        cif_property: str = "clbNonGroup.BAC_CIF",
        max_items: int = 50_000,
        page_size: int = 500,
    ) -> tuple[list[tuple[CmisChild, str]], bool]:
        """Documents of a given type filtered by their own CIF property.

        Unlike list_documents_of_type_in_folder this does not need an
        ancestor folder -- it queries the document type globally and
        returns each match together with the value of `cif_property`
        on that document, so the caller can route it to the right
        target folder.

        Pages through `skipCount` until the server is empty or
        `max_items` is reached. Returns `(items, hit_cap)` where
        `hit_cap` is True iff we stopped because we hit `max_items`
        rather than running out of results.
        """
        repo = self.repository(repository_id)
        type_qn, cif_qn = await self.resolve_query_names(
            repository_id, doc_type_id, cif_property
        )
        statement = (
            "SELECT cmis:objectId, cmis:name, cmis:contentStreamLength, "
            "cmis:contentStreamMimeType, cmis:lastModificationDate, "
            f"cmis:objectTypeId, {cif_qn} "
            f"FROM {type_qn}"
        )
        if cif:
            statement += f" WHERE {cif_qn} = {_q_literal(cif)}"

        out: list[tuple[CmisChild, str]] = []
        skip = 0
        hit_cap = False
        while len(out) < max_items:
            batch = min(page_size, max_items - len(out))
            data = {
                "cmisaction": "query",
                "statement": statement,
                "searchAllVersions": "false",
                "maxItems": str(batch),
                "skipCount": str(skip),
            }
            resp = await self._client.post(repo.repository_url, data=data)
            self._raise_for_status(resp)
            payload = resp.json()
            rows = payload.get("results", []) or []
            if not rows:
                break
            rows = rows[: max_items - len(out)]
            for row in rows:
                props = row.get("properties", {}) or row.get("succinctProperties", {})
                cif_val = _prop(props, cif_qn)
                if cif_val is None:
                    cif_val = _prop(props, cif_property)
                child = CmisChild(
                    object_id=_prop(props, "cmis:objectId") or "",
                    name=_prop(props, "cmis:name") or "",
                    is_folder=False,
                    content_stream_length=_to_int(_prop(props, "cmis:contentStreamLength")),
                    content_stream_mime_type=_prop(props, "cmis:contentStreamMimeType"),
                    last_modified=_prop(props, "cmis:lastModificationDate"),
                    object_type_id=_prop(props, "cmis:objectTypeId"),
                )
                out.append((child, "" if cif_val is None else str(cif_val)))
            skip += len(rows)
            if not payload.get("hasMoreItems", False):
                break
        else:
            hit_cap = True
        return out, hit_cap

    async def search_by_property(
        self,
        repository_id: str,
        property_id: str,
        value_substring: str,
        *,
        type_id: str | None = None,
        root_type_id: str = "cmis:folder",
        description_contains: str | None = None,
        max_items_per_type: int = 1000,
        page_size: int = 500,
    ) -> tuple[list[CmisSearchResult], list[tuple[str, str]]]:
        """Additive search across two independent paths, union of results.

        Path A (property): `SELECT ... FROM <type_qn> WHERE <prop_qn>
        LIKE '%VALUE%'`. Runs only if `value_substring` is non-empty.
        If `type_id` is given, queries just that type; otherwise walks
        the subtree of `root_type_id` and queries every "frontier"
        type that defines `property_id`.

        Path B (type description): finds every type under `root_type_id`
        whose type-definition `description` field contains
        `description_contains` (case-insensitive substring, matched
        client-side over the cached type tree). Runs only if
        `description_contains` is non-empty. Each matching type is
        queried with no WHERE clause -- returns every folder of that
        type. Property column comes back empty for these rows since the
        type may not even define the property.

        At least one of `value_substring` and `description_contains`
        must be non-empty.

        A type that matches BOTH paths is queried only via Path B, which
        is a superset, to avoid duplicate rows.

        `max_items_per_type` caps each individual query, NOT the total.

        Returns (hits, queried_types) where queried_types is a list of
        (type_id, type_queryName) pairs so the UI can show what was
        actually searched.
        """
        has_value = bool(value_substring)
        has_desc = bool(description_contains)
        if not has_value and not has_desc:
            return [], []

        prop_path_types: list[str] = []
        if has_value:
            if type_id:
                prop_path_types = [type_id]
            else:
                prop_path_types = await self.find_property_owning_types(
                    repository_id, property_id, root_type_id=root_type_id
                )
                if not prop_path_types and not has_desc:
                    raise CmisError(
                        f"No type under {root_type_id!r} defines property "
                        f"{property_id!r} in this repository. Try a different "
                        f"property id or pass an explicit Folder type."
                    )

        desc_path_types: list[str] = []
        if has_desc:
            desc_path_types = await self.find_types_by_description(
                repository_id, description_contains, root_type_id=root_type_id
            )
            if not desc_path_types and not prop_path_types:
                raise CmisError(
                    f"No type under {root_type_id!r} has a description "
                    f"matching {description_contains!r}."
                )

        # De-overlap: types in Path B are queried without WHERE, which
        # already covers any rows Path A would have returned for them.
        desc_set = set(desc_path_types)
        prop_only = [t for t in prop_path_types if t not in desc_set]

        escaped = value_substring.replace("'", "''") if has_value else ""
        repo = self.repository(repository_id)

        def base_type_of(typedef: dict[str, Any]) -> str:
            return str(typedef.get("baseId") or typedef.get("baseTypeId") or "")

        async def run_query(
            t_id: str, statement: str, default_is_folder: bool, prop_qn: str | None
        ) -> list[CmisSearchResult]:
            out: list[CmisSearchResult] = []
            skip = 0
            while len(out) < max_items_per_type:
                batch = min(page_size, max_items_per_type - len(out))
                data = {
                    "cmisaction": "query",
                    "statement": statement,
                    "searchAllVersions": "false",
                    "maxItems": str(batch),
                    "skipCount": str(skip),
                }
                resp = await self._client.post(repo.repository_url, data=data)
                if not resp.is_success:
                    raise CmisError(
                        f"HTTP {resp.status_code} for search statement "
                        f"{statement!r}: {resp.text[:500]}",
                        status_code=resp.status_code,
                        body=resp.text,
                    )
                payload = resp.json()
                rows = payload.get("results", []) or []
                if not rows:
                    break
                rows = rows[: max_items_per_type - len(out)]
                for row in rows:
                    props = row.get("properties", {}) or row.get("succinctProperties", {})
                    if prop_qn:
                        value = _prop(props, prop_qn)
                        if value is None:
                            value = _prop(props, property_id)
                    else:
                        value = None
                    base = _prop(props, "cmis:baseTypeId")
                    is_folder = (
                        base == "cmis:folder" if base else default_is_folder
                    )
                    out.append(
                        CmisSearchResult(
                            object_id=_prop(props, "cmis:objectId") or "",
                            name=_prop(props, "cmis:name") or "",
                            object_type_id=_prop(props, "cmis:objectTypeId") or t_id,
                            property_value="" if value is None else str(value),
                            is_folder=is_folder,
                        )
                    )
                skip += len(rows)
                if not payload.get("hasMoreItems", False):
                    break
            return out

        async def query_prop(t_id: str) -> tuple[str, str, list[CmisSearchResult]]:
            type_qn, prop_qn = await self.resolve_query_names(
                repository_id, t_id, property_id
            )
            typedef = await self.get_type_definition(repository_id, t_id)
            default_is_folder = base_type_of(typedef) == "cmis:folder"
            statement = (
                f"SELECT cmis:objectId, cmis:name, cmis:objectTypeId, "
                f"cmis:baseTypeId, {prop_qn} "
                f"FROM {type_qn} "
                f"WHERE {prop_qn} LIKE '%{escaped}%'"
            )
            out = await run_query(t_id, statement, default_is_folder, prop_qn)
            return type_qn, prop_qn, out

        async def query_desc(t_id: str) -> tuple[str, str, list[CmisSearchResult]]:
            typedef = await self.get_type_definition(repository_id, t_id)
            type_qn = typedef.get("queryName") or typedef.get("query_name")
            if not type_qn:
                raise CmisError(
                    f"Type {t_id!r} has no queryName; cannot include in "
                    f"description-path search."
                )
            default_is_folder = base_type_of(typedef) == "cmis:folder"
            statement = (
                f"SELECT cmis:objectId, cmis:name, cmis:objectTypeId, "
                f"cmis:baseTypeId "
                f"FROM {type_qn}"
            )
            out = await run_query(t_id, statement, default_is_folder, None)
            return str(type_qn), "", out

        tasks = [query_prop(t) for t in prop_only] + [
            query_desc(t) for t in desc_path_types
        ]
        labels = list(prop_only) + list(desc_path_types)
        results = await asyncio.gather(*tasks) if tasks else []

        hits: list[CmisSearchResult] = []
        queried: list[tuple[str, str]] = []
        for t_id, (type_qn, _prop_qn, batch) in zip(labels, results):
            queried.append((t_id, type_qn))
            hits.extend(batch)
        return hits, queried

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
