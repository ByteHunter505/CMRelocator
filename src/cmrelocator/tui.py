"""Textual TUI for CMRelocator."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Static,
)

from cmrelocator.cmis_client import CmisChild, CmisClient, CmisError


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

    #connection, #matching {
        height: auto;
        border: round $accent;
        padding: 0 1;
        margin: 1 1 0 1;
    }

    #actions {
        height: 3;
        padding: 0 1;
        margin: 0 1;
    }

    #docs {
        height: 1fr;
        margin: 0 1;
    }

    #status {
        height: auto;
        margin: 0 1;
    }

    #log {
        height: 12;
        border: round $accent;
        margin: 0 1 1 1;
    }

    Label.field {
        width: 20;
        content-align: left middle;
    }

    Input { width: 1fr; }
    Button { margin: 0 1 0 0; }
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
        self._client: CmisClient | None = None
        self._repo_id: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
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

            with Vertical(id="matching"):
                yield Static("[b]Matching[/b]")
                with Horizontal():
                    yield Label("Source Type ID", classes="field")
                    yield Input(placeholder="$p!-2_BAC_01_01_01_02v-1", id="source_type")
                with Horizontal():
                    yield Label("Target Type ID", classes="field")
                    yield Input(placeholder="$p!-2_BAC_01_01_01_02v-2", id="target_type")
                with Horizontal():
                    yield Label("CIF (optional)", classes="field")
                    yield Input(placeholder="empty = migrate all customers", id="cif")
                with Horizontal():
                    yield Label("Max items", classes="field")
                    yield Input(value="5000", id="max_docs", restrict=r"[0-9]*")
                with Horizontal():
                    yield Label("Max parallel", classes="field")
                    yield Input(value="4", id="concurrency", restrict=r"[0-9]*")
                with Horizontal():
                    yield Button("Query items", id="query", variant="primary")
                    yield Static("", id="query_status")

            yield DataTable(id="docs", zebra_stripes=True, cursor_type="row")

            with Horizontal(id="actions"):
                yield Button("Select all", id="select_all")
                yield Button("Deselect all", id="deselect_all")
                yield Button("Toggle row", id="toggle")
                yield Button("Migrate", id="migrate", variant="success")

            with Vertical(id="status"):
                yield ProgressBar(id="progress", show_eta=True)

            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#docs", DataTable)
        table.add_column("Sel", key="sel", width=4)
        table.add_column("CIF", key="cif", width=14)
        table.add_column("Kind", key="kind", width=5)
        table.add_column("Name", key="name", width=38)
        table.add_column("Size", key="size", width=12)
        table.add_column("MIME", key="mime", width=22)
        table.add_column("Modified", key="modified", width=22)
        table.add_column("Status", key="status")
        self._log("[dim]Ready. Fill connection fields and press Connect.[/dim]")

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

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

    @on(Button.Pressed, "#query")
    @work(exclusive=True)
    async def handle_query(self) -> None:
        if self._client is None:
            self._log("[red]Connect first.[/red]")
            return

        source_type = self.query_one("#source_type", Input).value.strip()
        target_type = self.query_one("#target_type", Input).value.strip()
        cif = self.query_one("#cif", Input).value.strip()
        max_docs = _safe_int(self.query_one("#max_docs", Input).value, default=5000, lo=1)
        concurrency = _safe_int(
            self.query_one("#concurrency", Input).value, default=4, lo=1, hi=16
        )

        if not source_type or not target_type:
            self._log("[red]Provide source and target Type IDs.[/red]")
            return

        status = self.query_one("#query_status", Static)
        status.update("[yellow]Querying folders...[/yellow]")

        try:
            source_folders = await self._client.list_folders_by_type(
                self._repo_id, source_type, cif=cif or None, max_items=max(1000, max_docs * 2)
            )
            target_folders = await self._client.list_folders_by_type(
                self._repo_id, target_type, cif=cif or None, max_items=max(1000, max_docs * 2)
            )
        except Exception as exc:
            status.update("[red]Folder query failed[/red]")
            self._log(f"[red]Folder query failed: {exc}[/red]")
            return

        if not source_folders:
            status.update("[red]No source folders found[/red]")
            self._log("[red]No source folders matched.[/red]")
            return

        target_by_cif: dict[str, str] = {}
        for tf in target_folders:
            if not tf.cif:
                continue
            if tf.cif in target_by_cif:
                self._log(
                    f"[yellow]Multiple target folders for CIF {tf.cif}; "
                    f"using first (1:1 assumption).[/yellow]"
                )
                continue
            target_by_cif[tf.cif] = tf.object_id

        pairs: list[tuple[str, str, str]] = []
        seen_cifs: set[str] = set()
        for sf in source_folders:
            if not sf.cif:
                self._log(
                    f"[yellow]Source folder {sf.object_id} has no CIF; skipping.[/yellow]"
                )
                continue
            if sf.cif in seen_cifs:
                self._log(
                    f"[yellow]Multiple source folders for CIF {sf.cif}; "
                    f"using first (1:1 assumption).[/yellow]"
                )
                continue
            seen_cifs.add(sf.cif)
            target_id = target_by_cif.get(sf.cif)
            if not target_id:
                self._log(
                    f"[yellow]CIF {sf.cif}: no matching target folder; skipping.[/yellow]"
                )
                continue
            pairs.append((sf.cif, sf.object_id, target_id))

        if not pairs:
            status.update("[red]No matching source/target pairs[/red]")
            self._log("[red]No source/target folder pairs found.[/red]")
            return

        status.update(
            f"[yellow]Listing items in {len(pairs)} folder(s)...[/yellow]"
        )

        sem = asyncio.Semaphore(concurrency)

        async def list_for(cif_v: str, source_id: str, target_id: str):
            async with sem:
                try:
                    children = await self._client.list_children(
                        self._repo_id, source_id
                    )
                except Exception as exc:
                    self._log(
                        f"[red]Failed to list folder {source_id} "
                        f"(CIF {cif_v}): {exc}[/red]"
                    )
                    return []
                return [(cif_v, source_id, target_id, c) for c in children]

        batches = await asyncio.gather(
            *(list_for(c, s, t) for c, s, t in pairs)
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
        self._log(
            "[dim]Folders will be moved with their entire subtree (CMIS moveObject).[/dim]"
        )
        self.query_one("#progress", ProgressBar).update(total=len(rows), progress=0)

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
                _fmt_size(row.item.content_stream_length) if not row.item.is_folder else "",
                row.item.content_stream_mime_type or "",
                (row.item.last_modified or "")[:19],
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
            row_key, _col_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return
        if row_key.value is None:
            return
        self._toggle_index(int(row_key.value))

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
            self._log("[yellow]No items selected (or all already moved).[/yellow]")
            return

        progress = self.query_one("#progress", ProgressBar)
        progress.update(total=len(targets), progress=0)
        self._log(
            f"[cyan]Migrating {len(targets)} items "
            f"(concurrency={concurrency})...[/cyan]"
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

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.close()


def _checkbox(selected: bool) -> str:
    return "[x]" if selected else "[ ]"


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


def _safe_int(raw: str, *, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v
