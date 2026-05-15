"""Textual TUI for CMRelocator."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from cmrelocator.cmis_client import (
    CmisChild,
    CmisClient,
    CmisError,
    CmisFolder,
    CmisSearchResult,
)


@dataclass
class ItemRow:
    item: CmisChild
    cif: str
    source_folder_id: str
    target_folder_id: str
    selected: bool = True
    status: str = "pending"
    error: str = ""


class CMRelocatorApp(App):
    TITLE = "CMRelocator"
    SUB_TITLE = "Move documents between folders in IBM Content Manager v8"

    CSS = """
    Screen { layout: vertical; }

    #conn-area {
        height: auto;
        max-height: 10;
    }

    #connection, #matching, #search-panel {
        height: auto;
        border: round $accent;
        padding: 0 1;
        margin: 1 1 0 1;
    }

    #migrate-form, #search-form {
        height: auto;
        max-height: 22;
    }

    /* Flatten input-style widgets to a single row each so that field
       rows do not vertically overflow into the next row. Textual's
       defaults give Input/Select/Checkbox a `border: tall transparent`
       that consumes 2 extra rows; we don't need it. */
    Input, Select {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
    }
    Checkbox {
        margin: 0 1;
        height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
    }

    #actions {
        height: 3;
        padding: 0 1;
        margin: 0 1;
    }

    #docs, #search_results {
        height: 1fr;
        min-height: 8;
        margin: 0 1;
        border: round $accent;
    }

    #progress {
        margin: 0 1;
    }

    #log {
        height: 8;
        border: round $accent;
        margin: 0 1 1 1;
    }

    Label.field {
        width: 20;
        content-align: left middle;
    }

    Button { margin: 0 1 0 0; }

    .hidden { display: none; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+a", "select_all", "Select all"),
        ("ctrl+d", "deselect_all", "Deselect all"),
        ("space", "toggle_row", "Toggle row"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[ItemRow] = []
        self.search_hits: list[CmisSearchResult] = []
        self._client: CmisClient | None = None
        self._repo_id: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # Connection panel (shared across tabs, always at top)
        with VerticalScroll(id="conn-area"):
            with Vertical(id="connection"):
                yield Static("[b]Connection[/b]")
                with Horizontal():
                    yield Label("Service URL", classes="field")
                    yield Input(placeholder="https://host/cmis/browser", id="service_url")
                with Horizontal():
                    yield Label("Repository ID", classes="field")
                    yield Input(placeholder="REPO1", id="repo_id")
                with Horizontal():
                    yield Label("Username", classes="field")
                    yield Input(placeholder="admin", id="username")
                with Horizontal():
                    yield Label("Password", classes="field")
                    yield Input(placeholder="********", password=True, id="password")
                with Horizontal():
                    yield Button("Connect", id="connect", variant="primary")
                    yield Static("[dim]Not connected[/dim]", id="conn_status")

        with TabbedContent(id="tabs", initial="tab_migrate"):
            with TabPane("Migrate", id="tab_migrate"):
                with VerticalScroll(id="migrate-form"):
                    with Vertical(id="matching"):
                        yield Static("[b]Matching[/b]")
                        with Horizontal():
                            yield Label("Source kind", classes="field")
                            yield Select(
                                options=[
                                    ("Folder (move all children)", "folder"),
                                    ("File (only docs of a type)", "file"),
                                ],
                                value="folder",
                                allow_blank=False,
                                id="source_kind",
                            )
                        with Horizontal():
                            yield Label("Source Type ID", classes="field")
                            yield Input(
                                placeholder="$p!-2_BAC_01_01_01_02v-1",
                                id="source_type",
                            )
                        with Horizontal():
                            yield Label("Target Type ID", classes="field")
                            yield Input(
                                placeholder="$p!-2_BAC_01_01_01_02v-2",
                                id="target_type",
                            )
                        with Horizontal(id="doc_type_row", classes="hidden"):
                            yield Label("Document Type ID", classes="field")
                            yield Input(
                                placeholder="document type to filter (file mode only)",
                                id="doc_type",
                            )
                        with Horizontal():
                            yield Label("CIF property", classes="field")
                            yield Input(value="clbNonGroup.BAC_CIF", id="cif_property")
                        with Horizontal():
                            yield Label("CIF (optional)", classes="field")
                            yield Input(
                                placeholder="empty = migrate all customers",
                                id="cif",
                            )
                        with Horizontal():
                            yield Label("Max items", classes="field")
                            yield Input(
                                value="5000", id="max_docs", restrict=r"[0-9]*"
                            )
                        with Horizontal():
                            yield Label("Max parallel", classes="field")
                            yield Input(
                                value="4", id="concurrency", restrict=r"[0-9]*"
                            )
                        yield Checkbox(
                            "Create target folder if it doesn't exist",
                            id="opt_create_target",
                        )
                        yield Checkbox(
                            "Delete empty source folder after migration",
                            id="opt_delete_source",
                        )
                        with Horizontal():
                            yield Button(
                                "Query items", id="query", variant="primary"
                            )
                            yield Static("", id="query_status")

                yield DataTable(id="docs", zebra_stripes=True, cursor_type="row")

                with Horizontal(id="actions"):
                    yield Button("Select all", id="select_all")
                    yield Button("Deselect all", id="deselect_all")
                    yield Button("Toggle row", id="toggle")
                    yield Button("Migrate", id="migrate", variant="success")

                yield ProgressBar(id="progress", show_eta=True)

            with TabPane("Search by name", id="tab_search"):
                with VerticalScroll(id="search-form"):
                    with Vertical(id="search-panel"):
                        yield Static("[b]Search by name[/b]")
                        with Horizontal():
                            yield Label("Name contains", classes="field")
                            yield Input(
                                placeholder="case-insensitive substring of cmis:name",
                                id="search_name",
                            )
                        with Horizontal():
                            yield Label("Max per kind", classes="field")
                            yield Input(
                                value="500",
                                id="search_max",
                                restrict=r"[0-9]*",
                            )
                        with Horizontal():
                            yield Button(
                                "Search", id="search_btn", variant="primary"
                            )
                            yield Static("", id="search_status")

                yield DataTable(
                    id="search_results", zebra_stripes=True, cursor_type="row"
                )

        yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        # Migrate-tab items table
        table = self.query_one("#docs", DataTable)
        table.add_column("Sel", key="sel", width=4)
        table.add_column("CIF", key="cif", width=14)
        table.add_column("Kind", key="kind", width=5)
        table.add_column("Name", key="name", width=38)
        table.add_column("Size", key="size", width=12)
        table.add_column("MIME", key="mime", width=22)
        table.add_column("Modified", key="modified", width=22)
        table.add_column("Status", key="status")

        # Search-tab results table
        sr = self.query_one("#search_results", DataTable)
        sr.add_column("Kind", key="kind", width=5)
        sr.add_column("Name", key="name", width=40)
        sr.add_column("Type ID", key="type", width=34)
        sr.add_column("ObjectId", key="object_id")

        self._log("[dim]Ready. Fill connection fields and press Connect.[/dim]")

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    # ===================== Connection =====================

    @on(Button.Pressed, "#connect")
    async def handle_connect(self) -> None:
        service_url = self.query_one("#service_url", Input).value.strip()
        username = self.query_one("#username", Input).value
        password = self.query_one("#password", Input).value
        repo_id = self.query_one("#repo_id", Input).value.strip()
        status = self.query_one("#conn_status", Static)

        if not (service_url and username and repo_id):
            self._log("[red]Missing service URL, username or repository ID.[/red]")
            return

        if self._client is not None:
            await self._client.close()

        self._client = CmisClient(service_url, username, password)
        self._repo_id = repo_id
        status.update("[yellow]Connecting...[/yellow]")
        try:
            repos = await self._client.fetch_repositories()
        except Exception as exc:
            status.update(f"[red]Failed: {exc}[/red]")
            self._log(f"[red]Connection failed: {exc}[/red]")
            return

        if repo_id not in repos:
            status.update(f"[red]Repository {repo_id!r} not found[/red]")
            self._log(f"[red]Available repositories: {list(repos)}[/red]")
            return

        info = repos[repo_id]
        status.update(
            f"[green]Connected: {info.name} ({info.product_name} {info.product_version})[/green]"
        )
        self._log(f"[green]Connected to {info.name}[/green]")

    # ===================== Source kind (folder vs file) =====================

    @on(Select.Changed, "#source_kind")
    def handle_source_kind_changed(self, event: Select.Changed) -> None:
        doc_type_row = self.query_one("#doc_type_row")
        if event.value == "file":
            doc_type_row.remove_class("hidden")
        else:
            doc_type_row.add_class("hidden")

    # ===================== Migrate tab: query =====================

    @on(Button.Pressed, "#query")
    @work(exclusive=True)
    async def handle_query(self) -> None:
        if self._client is None:
            self._log("[red]Connect first.[/red]")
            return

        source_kind = str(self.query_one("#source_kind", Select).value)
        source_type = self.query_one("#source_type", Input).value.strip()
        target_type = self.query_one("#target_type", Input).value.strip()
        doc_type = self.query_one("#doc_type", Input).value.strip()
        cif_property = (
            self.query_one("#cif_property", Input).value.strip()
            or "clbNonGroup.BAC_CIF"
        )
        cif = self.query_one("#cif", Input).value.strip()
        max_docs = _safe_int(
            self.query_one("#max_docs", Input).value, default=5000, lo=1
        )
        concurrency = _safe_int(
            self.query_one("#concurrency", Input).value, default=4, lo=1, hi=16
        )
        opt_create_target = self.query_one("#opt_create_target", Checkbox).value

        if not source_type or not target_type:
            self._log("[red]Provide source and target Type IDs.[/red]")
            return
        if source_kind == "file" and not doc_type:
            self._log(
                "[red]File mode: provide a Document Type ID to filter source items.[/red]"
            )
            return

        status = self.query_one("#query_status", Static)
        status.update("[yellow]Discovering folders (paginated)...[/yellow]")

        try:
            source_folders, src_hit_cap = await self._client.list_folders_by_type(
                self._repo_id,
                source_type,
                cif=cif or None,
                cif_property=cif_property,
            )
            target_folders, tgt_hit_cap = await self._client.list_folders_by_type(
                self._repo_id,
                target_type,
                cif=cif or None,
                cif_property=cif_property,
            )
        except Exception as exc:
            status.update("[red]Folder query failed[/red]")
            self._log(f"[red]Folder query failed: {exc}[/red]")
            return

        if not source_folders:
            status.update("[red]No source folders found[/red]")
            self._log("[red]No source folders matched.[/red]")
            return

        mode_label = "FILE" if source_kind == "file" else "FOLDER"
        self._log(
            f"[cyan]Discovery ({mode_label}):[/cyan] {len(source_folders)} source folders, "
            f"{len(target_folders)} target folders"
            + (
                " [yellow](source fetch hit cap of 50000)[/yellow]"
                if src_hit_cap
                else ""
            )
            + (
                " [yellow](target fetch hit cap of 50000)[/yellow]"
                if tgt_hit_cap
                else ""
            )
        )

        target_by_cif: dict[str, CmisFolder] = {}
        target_dupes = 0
        for tf in target_folders:
            if not tf.cif:
                continue
            if tf.cif in target_by_cif:
                target_dupes += 1
                continue
            target_by_cif[tf.cif] = tf

        source_by_cif: dict[str, CmisFolder] = {}
        source_dupes = 0
        source_no_cif = 0
        for sf in source_folders:
            if not sf.cif:
                source_no_cif += 1
                continue
            if sf.cif in source_by_cif:
                source_dupes += 1
                continue
            source_by_cif[sf.cif] = sf

        cifs_src = set(source_by_cif.keys())
        cifs_tgt = set(target_by_cif.keys())
        matched_cifs = cifs_src & cifs_tgt
        only_source = cifs_src - cifs_tgt
        only_target = cifs_tgt - cifs_src

        self._log(
            f"[cyan]Matching:[/cyan] {len(matched_cifs)} pairs ready  |  "
            f"orphans: {len(only_source)} src-only, "
            f"{len(only_target)} tgt-only (skipped)  |  "
            f"dupes ignored: {source_dupes} src, {target_dupes} tgt"
            + (
                f"  |  {source_no_cif} src folders without CIF (skipped)"
                if source_no_cif
                else ""
            )
        )

        # Optional: create target folders for src-only orphans.
        if opt_create_target and only_source:
            created, failed = await self._create_missing_targets(
                only_source=only_source,
                source_by_cif=source_by_cif,
                target_by_cif=target_by_cif,
                target_type=target_type,
                cif_property=cif_property,
            )
            if created:
                cifs_tgt = set(target_by_cif.keys())
                matched_cifs = cifs_src & cifs_tgt
                only_source = cifs_src - cifs_tgt
                self._log(
                    f"[green]Created {created} target folder(s)[/green]"
                    + (f", [red]{failed} failed[/red]" if failed else "")
                    + f" -> {len(matched_cifs)} pairs ready, "
                    f"{len(only_source)} src-only remain."
                )
            elif failed:
                self._log(
                    f"[red]Target creation: 0 created, {failed} failed.[/red]"
                )

        pairs: list[tuple[str, str, str]] = [
            (c, source_by_cif[c].object_id, target_by_cif[c].object_id)
            for c in sorted(matched_cifs)
        ]

        if not pairs:
            status.update("[red]No matching source/target pairs[/red]")
            self._log(
                "[red]No source/target folder pairs found. Source and target "
                "types don't share CIFs.[/red]"
            )
            return

        status.update(
            f"[yellow]Listing items in {len(pairs)} folder(s) ({mode_label})...[/yellow]"
        )

        sem = asyncio.Semaphore(concurrency)

        async def list_for(
            cif_v: str, source_id: str, target_id: str
        ) -> list[tuple[str, str, str, CmisChild]]:
            async with sem:
                try:
                    if source_kind == "file":
                        items = await self._client.list_documents_of_type_in_folder(
                            self._repo_id, source_id, doc_type
                        )
                    else:
                        items = await self._client.list_children(
                            self._repo_id, source_id
                        )
                except Exception as exc:
                    self._log(
                        f"[red]Failed to list folder {source_id} "
                        f"(CIF {cif_v}): {exc}[/red]"
                    )
                    return []
                return [(cif_v, source_id, target_id, c) for c in items]

        batches = await asyncio.gather(
            *(list_for(c, s, t) for c, s, t in pairs)
        )

        total_returned = sum(len(b) for b in batches)
        empty_pairs = sum(1 for b in batches if not b)
        listing_func = (
            "list_documents_of_type_in_folder"
            if source_kind == "file"
            else "list_children"
        )
        self._log(
            f"[dim]{listing_func}: {total_returned} item(s) across "
            f"{len(pairs) - empty_pairs}/{len(pairs)} non-empty pair(s); "
            f"{empty_pairs} pair(s) had no matching items.[/dim]"
        )

        rows: list[ItemRow] = []
        truncated = False
        for batch in batches:
            for cif_v, src, tgt, child in batch:
                if len(rows) >= max_docs:
                    truncated = True
                    break
                rows.append(
                    ItemRow(
                        item=child,
                        cif=cif_v,
                        source_folder_id=src,
                        target_folder_id=tgt,
                        selected=True,
                    )
                )
            if truncated:
                break

        rows.sort(key=lambda r: (r.cif, not r.item.is_folder, r.item.name))
        self.rows = rows
        self._log(
            f"[dim]Rendering {len(self.rows)} row(s) in the table.[/dim]"
        )
        self._rebuild_table()

        unique_cifs = len({r.cif for r in rows})
        n_folders = sum(1 for r in rows if r.item.is_folder)
        n_docs = len(rows) - n_folders
        summary = (
            f"{len(rows)} items ({n_folders} folders, {n_docs} docs) "
            f"across {unique_cifs} CIF(s)"
        )
        if truncated:
            summary += f" (truncated at max_items={max_docs})"
            self._log(
                f"[yellow]Result truncated at max_items={max_docs}. "
                f"Raise the cap or filter by CIF to migrate the rest.[/yellow]"
            )
        status.update(f"[green]{summary}[/green]")
        self._log(f"[green]{summary}[/green]")
        if rows:
            self._log(
                "[dim]All items start pre-selected (green [x]). Click a row "
                "(or Space on the focused row) to deselect. Use the "
                "'Select all' / 'Deselect all' buttons for bulk. "
                "Migrate only moves rows currently marked [x].[/dim]"
            )
        if source_kind == "folder":
            self._log(
                "[dim]Folders will be moved with their entire subtree (CMIS moveObject).[/dim]"
            )
        self.query_one("#progress", ProgressBar).update(
            total=len(rows), progress=0
        )

    # ===================== Migrate tab: table =====================

    def _rebuild_table(self) -> None:
        table = self.query_one("#docs", DataTable)
        table.clear()
        for idx, row in enumerate(self.rows):
            kind = "[F]" if row.item.is_folder else "[D]"
            table.add_row(
                _checkbox(row.selected),
                row.cif,
                kind,
                row.item.name,
                _fmt_size(row.item.content_stream_length)
                if not row.item.is_folder
                else "",
                row.item.content_stream_mime_type or "",
                _fmt_modified(row.item.last_modified),
                _status_text(row.status, row.error),
                key=str(idx),
            )

    def _update_row(self, idx: int) -> None:
        table = self.query_one("#docs", DataTable)
        row = self.rows[idx]
        key = str(idx)
        try:
            table.update_cell(key, "sel", _checkbox(row.selected))
            table.update_cell(key, "status", _status_text(row.status, row.error))
        except Exception:
            pass

    def _toggle_index(self, idx: int) -> None:
        self.rows[idx].selected = not self.rows[idx].selected
        self._update_row(idx)

    @on(DataTable.RowSelected, "#docs")
    def handle_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        self._toggle_index(int(event.row_key.value))

    @on(Click, "#docs")
    def handle_click_on_docs(self, event: Click) -> None:
        if not self.rows:
            return
        self.call_after_refresh(self.action_toggle_row)

    @on(Button.Pressed, "#select_all")
    def handle_select_all(self) -> None:
        self.action_select_all()

    @on(Button.Pressed, "#deselect_all")
    def handle_deselect_all(self) -> None:
        self.action_deselect_all()

    @on(Button.Pressed, "#toggle")
    def handle_toggle_button(self) -> None:
        self.action_toggle_row()

    def action_select_all(self) -> None:
        for idx, r in enumerate(self.rows):
            r.selected = True
            self._update_row(idx)

    def action_deselect_all(self) -> None:
        for idx, r in enumerate(self.rows):
            r.selected = False
            self._update_row(idx)

    def action_toggle_row(self) -> None:
        table = self.query_one("#docs", DataTable)
        if not self.rows:
            return
        try:
            row_key, _col_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            )
        except Exception:
            return
        if row_key.value is None:
            return
        self._toggle_index(int(row_key.value))

    # ===================== Migrate tab: migrate =====================

    @on(Button.Pressed, "#migrate")
    @work(exclusive=True)
    async def handle_migrate(self) -> None:
        if self._client is None:
            self._log("[red]Connect first.[/red]")
            return
        concurrency = _safe_int(
            self.query_one("#concurrency", Input).value, default=4, lo=1, hi=16
        )

        targets = [
            (idx, row)
            for idx, row in enumerate(self.rows)
            if row.selected and row.status != "done"
        ]
        if not targets:
            self._log(
                "[yellow]No items selected (or all already moved).[/yellow]"
            )
            return

        progress = self.query_one("#progress", ProgressBar)
        progress.update(total=len(targets), progress=0)
        self._log(
            f"[cyan]Migrating {len(targets)} items (concurrency={concurrency})...[/cyan]"
        )

        sem = asyncio.Semaphore(concurrency)
        counters = {"ok": 0, "fail": 0}

        async def move_one(idx: int, row: ItemRow) -> None:
            async with sem:
                row.status = "moving"
                self._update_row(idx)
                kind = "[F]" if row.item.is_folder else "[D]"
                try:
                    await self._client.move_object(  # type: ignore[union-attr]
                        self._repo_id,
                        row.item.object_id,
                        row.source_folder_id,
                        row.target_folder_id,
                    )
                    row.status = "done"
                    counters["ok"] += 1
                    self._log(
                        f"[green]OK[/green]   CIF {row.cif} {kind} {row.item.name}"
                    )
                except CmisError as exc:
                    row.status = "error"
                    row.error = str(exc)
                    counters["fail"] += 1
                    self._log(
                        f"[red]FAIL[/red] CIF {row.cif} {kind} {row.item.name}: {exc}"
                    )
                except Exception as exc:
                    row.status = "error"
                    row.error = repr(exc)
                    counters["fail"] += 1
                    self._log(
                        f"[red]FAIL[/red] CIF {row.cif} {kind} {row.item.name}: {exc!r}"
                    )
                finally:
                    self._update_row(idx)
                    progress.advance(1)

        await asyncio.gather(*(move_one(i, r) for i, r in targets))
        self._log(
            f"[bold]Done.[/bold] ok={counters['ok']}  fail={counters['fail']}  "
            f"total={len(targets)}"
        )

        if self.query_one("#opt_delete_source", Checkbox).value:
            await self._cleanup_empty_sources(targets)

    # ===================== Search tab =====================

    @on(Button.Pressed, "#search_btn")
    @work(exclusive=True)
    async def handle_search(self) -> None:
        if self._client is None:
            self._log("[red]Connect first.[/red]")
            return
        term = self.query_one("#search_name", Input).value.strip()
        if not term:
            self._log("[red]Type a name substring to search for.[/red]")
            return
        max_per_kind = _safe_int(
            self.query_one("#search_max", Input).value,
            default=500,
            lo=1,
            hi=10_000,
        )

        status = self.query_one("#search_status", Static)
        status.update(
            "[yellow]Searching folders + documents (UPPER LIKE)...[/yellow]"
        )
        self._log(
            f"[cyan]Search:[/cyan] looking for '{term}' (case-insensitive substring) "
            f"in both folders and documents, up to {max_per_kind} per kind..."
        )

        try:
            hits = await self._client.search_objects_by_name(
                self._repo_id, term, max_items_per_kind=max_per_kind
            )
        except Exception as exc:
            status.update("[red]Search failed[/red]")
            self._log(f"[red]Search failed: {exc}[/red]")
            return

        # Sort: folders first, then documents; within each, by name.
        hits.sort(key=lambda h: (not h.is_folder, h.name.lower()))
        self.search_hits = hits

        n_folders = sum(1 for h in hits if h.is_folder)
        n_docs = len(hits) - n_folders
        status.update(
            f"[green]{len(hits)} hit(s) -- {n_folders} folders, {n_docs} docs[/green]"
        )
        self._log(
            f"[green]Search done.[/green] {len(hits)} hit(s): "
            f"{n_folders} folder(s), {n_docs} document(s)."
        )

        table = self.query_one("#search_results", DataTable)
        table.clear()
        for idx, h in enumerate(hits):
            kind = "[F]" if h.is_folder else "[D]"
            table.add_row(
                kind, h.name, h.object_type_id, h.object_id, key=str(idx)
            )

        if hits:
            self._log(
                "[dim]Click a row in the results table to see the full "
                "ObjectId echoed in this log (easy to copy).[/dim]"
            )

    @on(DataTable.RowSelected, "#search_results")
    def handle_search_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        # Echo the full ObjectId to the log so the user can copy it cleanly.
        if event.row_key.value is None:
            return
        try:
            idx = int(event.row_key.value)
        except (TypeError, ValueError):
            return
        if not (0 <= idx < len(self.search_hits)):
            return
        h = self.search_hits[idx]
        kind = "folder" if h.is_folder else "document"
        self._log(
            f"[cyan]{kind}[/cyan]  name={h.name!r}  type={h.object_type_id}  "
            f"objectId=[bold]{h.object_id}[/bold]"
        )

    # ===================== Helpers =====================

    async def _create_missing_targets(
        self,
        *,
        only_source: set[str],
        source_by_cif: dict[str, CmisFolder],
        target_by_cif: dict[str, CmisFolder],
        target_type: str,
        cif_property: str,
    ) -> tuple[int, int]:
        """Create target folders for CIFs that exist only in source."""
        if not only_source:
            return 0, 0
        if not target_by_cif:
            self._log(
                "[yellow]Create-target enabled but no existing target folder to "
                "derive the parent from. Manually create one target folder of "
                "the right type first, then re-run.[/yellow]"
            )
            return 0, 0

        sample = next(iter(target_by_cif.values()))
        try:
            parents = await self._client.get_object_parents(  # type: ignore[union-attr]
                self._repo_id, sample.object_id
            )
        except Exception as exc:
            self._log(
                f"[red]Could not fetch parent of an existing target: {exc}[/red]"
            )
            return 0, 0
        if not parents:
            self._log(
                "[yellow]Existing target folder has no discoverable parent; "
                "cannot create new targets.[/yellow]"
            )
            return 0, 0
        parent_id = parents[0]
        self._log(
            f"[cyan]Creating {len(only_source)} target folder(s) under "
            f"parent {parent_id}...[/cyan]"
        )

        created = 0
        failed = 0
        for cif_v in sorted(only_source):
            sf = source_by_cif[cif_v]
            try:
                new_id = await self._client.create_folder(  # type: ignore[union-attr]
                    self._repo_id,
                    parent_id,
                    sf.name,
                    target_type,
                    custom_properties={cif_property: cif_v},
                )
                target_by_cif[cif_v] = CmisFolder(
                    object_id=new_id,
                    name=sf.name,
                    cif=cif_v,
                    object_type_id=target_type,
                )
                created += 1
            except Exception as exc:
                failed += 1
                self._log(
                    f"[red]Create target for CIF {cif_v} failed: {exc}[/red]"
                )
        return created, failed

    async def _cleanup_empty_sources(
        self, migrated_rows: list[tuple[int, ItemRow]]
    ) -> None:
        """For each source folder touched during this migration, delete it
        if it's now empty."""
        touched = {row.source_folder_id for _, row in migrated_rows}
        if not touched:
            return
        self._log(
            f"[cyan]Cleanup:[/cyan] checking {len(touched)} source folder(s) "
            f"for emptiness..."
        )
        deleted = 0
        not_empty = 0
        errored = 0
        for src_id in touched:
            try:
                remaining = await self._client.list_children(  # type: ignore[union-attr]
                    self._repo_id, src_id, max_items=1
                )
                if remaining:
                    not_empty += 1
                    continue
                await self._client.delete_object(  # type: ignore[union-attr]
                    self._repo_id, src_id
                )
                deleted += 1
                self._log(f"[green]Deleted empty source[/green] {src_id}")
            except Exception as exc:
                errored += 1
                self._log(f"[red]Cleanup failed for {src_id}: {exc}[/red]")
        self._log(
            f"[bold]Cleanup done.[/bold] deleted={deleted}  "
            f"still_had_content={not_empty}  errored={errored}"
        )

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.close()


def _checkbox(selected: bool) -> Text:
    if selected:
        return Text("[x]", style="bold green")
    return Text("[ ]", style="dim")


def _fmt_size(size: int | None) -> str:
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _fmt_modified(value) -> str:
    """Format a CMIS lastModificationDate; tolerates ms-since-epoch ints,
    ISO strings, None, and empty."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        try:
            seconds = float(value) / 1000.0
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)[:19]


def _status_text(status: str, error: str) -> Text:
    if status == "pending":
        return Text("pending", style="dim")
    if status == "moving":
        return Text("moving...", style="yellow")
    if status == "done":
        return Text("OK", style="bold green")
    if status == "error":
        return Text(f"ERR {error[:40]}", style="bold red")
    return Text(status)


def _safe_int(
    raw: str,
    *,
    default: int,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v
