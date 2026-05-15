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
        max-height: 24;
    }

    /* Flatten Input/Checkbox to a single row each so that field rows
       do not vertically overflow into the next row. Textual's defaults
       give them a `border: tall transparent` that consumes 2 extra rows;
       we don't need it. Select is intentionally NOT flattened: its inner
       SelectCurrent renders the selected value inside a 3-row frame, and
       collapsing the outer Select hides that value. */
    Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
    }
    Select {
        width: 1fr;
    }
    Checkbox {
        margin: 0 1;
        height: 1;
        border: none;
        padding: 0 1;
        background: transparent;
    }

    /* The one row that holds a Select keeps its native 3-row height. */
    #source_kind_row { height: 3; }
    #search_scope_row { height: 3; }

    #selected_type_row {
        height: 1;
        margin: 0 1;
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
        # Each row in the dedup table aggregates 1..N raw hits sharing the
        # same (name, property_value, type_id, is_folder). Preserves the
        # object_ids per group so a row click can still report the
        # underlying ids.
        self.search_groups: list[tuple[tuple[str, str, str, bool], list[CmisSearchResult]]] = []
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
                        with Horizontal(id="source_kind_row"):
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
                                placeholder="folder mode: required.  file mode: optional - leave empty to find docs globally by their CIF property",
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

            with TabPane("Search", id="tab_search"):
                with VerticalScroll(id="search-form"):
                    with Vertical(id="search-panel"):
                        yield Static("[b]Search by property and/or type description[/b]")
                        with Horizontal(id="search_scope_row"):
                            yield Label("Search scope", classes="field")
                            yield Select(
                                options=[
                                    ("Folders", "folder"),
                                    ("Documents", "document"),
                                    ("Both", "both"),
                                ],
                                value="folder",
                                allow_blank=False,
                                id="search_scope",
                            )
                        with Horizontal():
                            yield Label("Type ID", classes="field")
                            yield Input(
                                placeholder="(optional - leave empty to auto-discover every type in scope that has this property)",
                                id="search_type",
                            )
                        with Horizontal():
                            yield Label("Property", classes="field")
                            yield Input(
                                value="clbNonGroup.BAC_Nombre_Carpeta",
                                id="search_property",
                            )
                        with Horizontal():
                            yield Label("Value contains", classes="field")
                            yield Input(
                                placeholder="optional - case-sensitive substring (LIKE %value%)",
                                id="search_value",
                            )
                        with Horizontal():
                            yield Label("Type desc contains", classes="field")
                            yield Input(
                                placeholder="optional - case-insensitive substring on the type's description (e.g. FIRMAS)",
                                id="search_desc",
                            )
                        with Horizontal():
                            yield Label("Max results", classes="field")
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

                with Horizontal(id="selected_type_row"):
                    yield Label("Selected Type ID", classes="field")
                    yield Input(
                        placeholder="click a result row to populate (select with mouse + Ctrl+C / Cmd+C to copy)",
                        id="selected_type_id",
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

        # Search-tab results table. ObjectId is intentionally omitted --
        # the user requested distinct rows over the 4 displayed columns
        # only, and a row click echoes the underlying object_id(s) plus
        # populates the "Selected Type ID" input for copy/paste.
        sr = self.query_one("#search_results", DataTable)
        sr.add_column("Name (cmis:name)", key="name", width=28)
        sr.add_column("Property value", key="value", width=34)
        sr.add_column("Type ID", key="type", width=28)
        sr.add_column("Kind", key="kind", width=6)

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
        # Wipe any previous query's results before validating so that
        # a failed/early-returning Query also clears stale rows.
        self.rows = []
        self.query_one("#docs", DataTable).clear()
        self.query_one("#progress", ProgressBar).update(total=0, progress=0)

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

        if not target_type:
            self._log("[red]Provide a Target Type ID.[/red]")
            return
        if source_kind == "folder" and not source_type:
            self._log(
                "[red]Folder mode: Source Type ID is required.[/red]"
            )
            return
        if source_kind == "file" and not doc_type:
            self._log(
                "[red]File mode: provide a Document Type ID to filter source items.[/red]"
            )
            return

        # File mode + no Source Type: docs are discovered globally by
        # their own CIF property, parents are looked up afterwards,
        # and rows are routed to target folders by CIF. Different
        # enough from the source-folder-driven flow that we branch
        # out into a dedicated helper.
        if source_kind == "file" and not source_type:
            await self._query_file_by_doc_cif(
                doc_type=doc_type,
                target_type=target_type,
                cif=cif,
                cif_property=cif_property,
                max_docs=max_docs,
                concurrency=concurrency,
                opt_create_target=opt_create_target,
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
        # Wipe any previous search results before validating so a failed
        # / early-returning Search also clears stale rows.
        self.search_hits = []
        self.search_groups = []
        self.query_one("#search_results", DataTable).clear()
        self.query_one("#selected_type_id", Input).value = ""

        if self._client is None:
            self._log("[red]Connect first.[/red]")
            return
        type_id = self.query_one("#search_type", Input).value.strip() or None
        prop_id = self.query_one("#search_property", Input).value.strip()
        value = self.query_one("#search_value", Input).value.strip()
        desc = self.query_one("#search_desc", Input).value.strip()
        scope = str(self.query_one("#search_scope", Select).value)
        if not prop_id:
            self._log("[red]Provide the property to search.[/red]")
            return
        if not value and not desc:
            self._log(
                "[red]Provide a Value substring, a Type description "
                "substring, or both. Leaving them both empty would match "
                "the entire repository.[/red]"
            )
            return
        max_items = _safe_int(
            self.query_one("#search_max", Input).value,
            default=500,
            lo=1,
            hi=10_000,
        )

        # An explicit Type ID pins the FROM clause -- "scope" is moot,
        # and walking both trees with the same type_id would double-
        # query it. Auto-discovery is the case where scope matters.
        if type_id:
            roots = ["cmis:folder"]  # ignored anyway; needed as a single iteration
        elif scope == "both":
            roots = ["cmis:folder", "cmis:document"]
        elif scope == "document":
            roots = ["cmis:document"]
        else:
            roots = ["cmis:folder"]

        status = self.query_one("#search_status", Static)
        status.update("[yellow]Searching...[/yellow]")
        clauses: list[str] = []
        if value:
            target = (
                f"type={type_id}"
                if type_id
                else f"auto-discover types defining property={prop_id}"
            )
            clauses.append(f"property LIKE '%{value}%' on {target}")
        if desc:
            clauses.append(f"type description contains {desc!r}")
        self._log(
            f"[cyan]Search[/cyan] scope={scope}, "
            f"{' OR '.join(clauses)}, max {max_items} per type"
        )

        try:
            # `find_*` failures are different per root (e.g. property
            # exists under cmis:folder but not under cmis:document). With
            # multiple roots we don't want one barren tree to abort the
            # whole search, so swallow CmisError per root and keep going.
            per_root = await asyncio.gather(
                *(
                    self._search_one_root(prop_id, value, type_id, desc, root, max_items)
                    for root in roots
                ),
                return_exceptions=True,
            )
            hits: list[CmisSearchResult] = []
            queried: list[tuple[str, str]] = []
            errors: list[str] = []
            for root, result in zip(roots, per_root):
                if isinstance(result, Exception):
                    errors.append(f"{root}: {result}")
                    continue
                root_hits, root_queried = result
                hits.extend(root_hits)
                queried.extend(root_queried)
            if errors and not hits:
                status.update("[red]Search failed[/red]")
                for err in errors:
                    self._log(f"[red]Search failed: {err}[/red]")
                return
            for err in errors:
                self._log(f"[yellow]Partial: {err}[/yellow]")
        except Exception as exc:
            status.update("[red]Search failed[/red]")
            self._log(f"[red]Search failed: {exc}[/red]")
            return

        if queried:
            summary = ", ".join(f"{t}->{qn}" for t, qn in queried)
            self._log(f"[dim]Queried {len(queried)} type(s): {summary}[/dim]")
        self.search_hits = hits

        # Distinct over (name, property_value, type_id, is_folder) while
        # preserving the underlying object_ids so a row click can still
        # report them. dict insertion order keeps a stable presentation.
        groups: dict[tuple[str, str, str, bool], list[CmisSearchResult]] = {}
        for h in hits:
            key = (h.name, h.property_value, h.object_type_id, h.is_folder)
            groups.setdefault(key, []).append(h)
        ordered = sorted(
            groups.items(),
            key=lambda kv: (
                kv[0][1].lower(),  # property value
                kv[0][0].lower(),  # name
                kv[0][2],          # type id
            ),
        )
        self.search_groups = ordered

        status.update(
            f"[green]{len(ordered)} distinct row(s) "
            f"({len(hits)} raw hit(s))[/green]"
        )
        self._log(
            f"[green]Search done.[/green] {len(ordered)} distinct row(s) "
            f"out of {len(hits)} raw hit(s)."
        )

        table = self.query_one("#search_results", DataTable)
        table.clear()
        for idx, ((name, val, t_id, is_folder), bucket) in enumerate(ordered):
            kind = "[F]" if is_folder else "[D]"
            label = (
                f"{name} ({len(bucket)})" if len(bucket) > 1 else name
            )
            table.add_row(label, val, t_id, kind, key=str(idx))

        if ordered:
            self._log(
                "[dim]Click a row to copy its Type ID into the "
                "'Selected Type ID' input and echo the underlying "
                "ObjectId(s) to this log.[/dim]"
            )

    @on(DataTable.RowSelected, "#search_results")
    def handle_search_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        if event.row_key.value is None:
            return
        try:
            idx = int(event.row_key.value)
        except (TypeError, ValueError):
            return
        if not (0 <= idx < len(self.search_groups)):
            return
        (name, val, t_id, is_folder), bucket = self.search_groups[idx]
        self.query_one("#selected_type_id", Input).value = t_id
        kind = "folder" if is_folder else "document"
        ids = ", ".join(h.object_id for h in bucket)
        self._log(
            f"name={name!r}  property_value={val!r}  "
            f"type=[bold]{t_id}[/bold]  kind={kind}  "
            f"objectId(s)=[bold]{ids}[/bold]"
        )

    # ===================== Helpers =====================

    async def _search_one_root(
        self,
        prop_id: str,
        value: str,
        type_id: str | None,
        desc: str,
        root_type_id: str,
        max_items: int,
    ) -> tuple[list[CmisSearchResult], list[tuple[str, str]]]:
        return await self._client.search_by_property(  # type: ignore[union-attr]
            self._repo_id,
            prop_id,
            value,
            type_id=type_id,
            root_type_id=root_type_id,
            description_contains=desc or None,
            max_items_per_type=max_items,
        )

    async def _query_file_by_doc_cif(
        self,
        *,
        doc_type: str,
        target_type: str,
        cif: str,
        cif_property: str,
        max_docs: int,
        concurrency: int,
        opt_create_target: bool,
    ) -> None:
        """File-mode discovery without a Source Type.

        Queries documents of `doc_type` filtered by their own CIF
        property, looks up the per-CIF target folder of `target_type`,
        and resolves each document's parent folder to satisfy
        moveObject's sourceFolderId requirement.
        """
        status = self.query_one("#query_status", Static)
        status.update(
            "[yellow]Discovering documents directly by CIF (paginated)...[/yellow]"
        )

        try:
            doc_pairs, doc_hit_cap = await self._client.list_documents_of_type_by_cif(  # type: ignore[union-attr]
                self._repo_id,
                doc_type,
                cif=cif or None,
                cif_property=cif_property,
            )
            target_folders, tgt_hit_cap = await self._client.list_folders_by_type(  # type: ignore[union-attr]
                self._repo_id,
                target_type,
                cif=cif or None,
                cif_property=cif_property,
            )
        except Exception as exc:
            status.update("[red]Discovery failed[/red]")
            self._log(f"[red]Discovery failed: {exc}[/red]")
            return

        if not doc_pairs:
            status.update("[red]No documents found[/red]")
            self._log(
                "[red]No documents of that type matched. Check the "
                "Document Type ID and the CIF property name.[/red]"
            )
            return

        self._log(
            f"[cyan]Discovery (FILE-by-doc-CIF):[/cyan] "
            f"{len(doc_pairs)} document(s), {len(target_folders)} target folder(s)"
            + (
                " [yellow](doc fetch hit cap of 50000)[/yellow]"
                if doc_hit_cap
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

        # Group docs by CIF for the orphan-target creation step and
        # for cleaner logging. Docs without a CIF can't be routed.
        docs_by_cif: dict[str, list[CmisChild]] = {}
        docs_no_cif = 0
        for child, doc_cif in doc_pairs:
            if not doc_cif:
                docs_no_cif += 1
                continue
            docs_by_cif.setdefault(doc_cif, []).append(child)

        cifs_with_docs = set(docs_by_cif.keys())
        cifs_tgt = set(target_by_cif.keys())
        routable_cifs = cifs_with_docs & cifs_tgt
        only_doc_cifs = cifs_with_docs - cifs_tgt

        # Optional: synthesise target folders for CIFs that have docs
        # but no existing target. Mirrors the folder-mode flow but
        # reuses any existing target folder as a parent-lookup probe.
        if opt_create_target and only_doc_cifs and target_by_cif:
            # The helper expects source_by_cif, so synthesise minimal
            # CmisFolder stubs with just the CIF and the doc's name --
            # the helper only reads `name` and `cif`.
            source_stubs: dict[str, CmisFolder] = {}
            for cif_v in only_doc_cifs:
                first_doc = docs_by_cif[cif_v][0]
                source_stubs[cif_v] = CmisFolder(
                    object_id="",
                    name=first_doc.name or cif_v,
                    cif=cif_v,
                    object_type_id="",
                )
            created, failed = await self._create_missing_targets(
                only_source=set(only_doc_cifs),
                source_by_cif=source_stubs,
                target_by_cif=target_by_cif,
                target_type=target_type,
                cif_property=cif_property,
            )
            if created:
                cifs_tgt = set(target_by_cif.keys())
                routable_cifs = cifs_with_docs & cifs_tgt
                only_doc_cifs = cifs_with_docs - cifs_tgt
                self._log(
                    f"[green]Created {created} target folder(s)[/green]"
                    + (f", [red]{failed} failed[/red]" if failed else "")
                    + f" -> {len(routable_cifs)} CIF(s) now routable."
                )
            elif failed:
                self._log(
                    f"[red]Target creation: 0 created, {failed} failed.[/red]"
                )

        routable_docs: list[tuple[str, CmisChild]] = []
        unrouted_docs = 0
        for cif_v, docs in docs_by_cif.items():
            if cif_v not in target_by_cif:
                unrouted_docs += len(docs)
                continue
            for d in docs:
                routable_docs.append((cif_v, d))

        self._log(
            f"[cyan]Routing:[/cyan] "
            f"{len(routable_docs)} doc(s) into {len(routable_cifs)} CIF(s)  |  "
            f"unrouted: {unrouted_docs} (no target folder for the CIF)"
            + (
                f"  |  {docs_no_cif} doc(s) without a CIF (skipped)"
                if docs_no_cif
                else ""
            )
            + (
                f"  |  {target_dupes} dup target(s) ignored"
                if target_dupes
                else ""
            )
        )

        if not routable_docs:
            status.update("[red]Nothing to migrate[/red]")
            self._log(
                "[red]No document has a matching target folder. Either "
                "create the target folders or enable "
                "'Create target folder if it doesn't exist'.[/red]"
            )
            return

        status.update(
            f"[yellow]Resolving parent folders for {len(routable_docs)} doc(s)...[/yellow]"
        )

        sem = asyncio.Semaphore(concurrency)

        async def resolve_parent(
            cif_v: str, doc: CmisChild
        ) -> tuple[str, CmisChild, str | None]:
            async with sem:
                try:
                    parents = await self._client.get_object_parents(  # type: ignore[union-attr]
                        self._repo_id, doc.object_id
                    )
                except Exception as exc:
                    self._log(
                        f"[red]Parent lookup failed for {doc.object_id} "
                        f"(CIF {cif_v}, {doc.name}): {exc}[/red]"
                    )
                    return cif_v, doc, None
                # Document may be multi-filed; CMIS moveObject removes
                # the link from one parent at a time. First parent is a
                # reasonable default for the single-parent common case.
                return cif_v, doc, parents[0] if parents else None

        resolved = await asyncio.gather(
            *(resolve_parent(c, d) for c, d in routable_docs)
        )

        rows: list[ItemRow] = []
        no_parent = 0
        multi_parent_warn = 0
        truncated = False
        for cif_v, doc, parent_id in resolved:
            if not parent_id:
                no_parent += 1
                continue
            if len(rows) >= max_docs:
                truncated = True
                break
            rows.append(
                ItemRow(
                    item=doc,
                    cif=cif_v,
                    source_folder_id=parent_id,
                    target_folder_id=target_by_cif[cif_v].object_id,
                    selected=True,
                )
            )

        if no_parent:
            self._log(
                f"[yellow]{no_parent} doc(s) had no resolvable parent "
                f"and were skipped.[/yellow]"
            )
        if multi_parent_warn:
            self._log(
                f"[yellow]{multi_parent_warn} doc(s) are multi-filed; "
                f"only the first parent will be the source folder for "
                f"moveObject.[/yellow]"
            )

        rows.sort(key=lambda r: (r.cif, r.item.name))
        self.rows = rows
        self._rebuild_table()

        unique_cifs = len({r.cif for r in rows})
        summary = (
            f"{len(rows)} doc(s) across {unique_cifs} CIF(s)"
        )
        if truncated:
            summary += f" (truncated at max_items={max_docs})"
            self._log(
                f"[yellow]Result truncated at max_items={max_docs}. "
                f"Raise the cap or filter by CIF to migrate the rest.[/yellow]"
            )
        status.update(f"[green]{summary}[/green]")
        self._log(f"[green]{summary}[/green]")
        self.query_one("#progress", ProgressBar).update(
            total=len(rows), progress=0
        )

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
