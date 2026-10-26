"""The evidence service: every tool of contract section 4 as a plain Python method.

`EvidenceService(run_dir)` serves one run folder. The MCP tools in `tools.py` are
thin wrappers over these methods; part B's register uses `stemma.evidence.api`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import time
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from stemma.evidence import charset, limits, sec_outline
from stemma.evidence.archive import Archive
from stemma.evidence.cache import RequestCache, request_key
from stemma.evidence.errors import HAS_TEXT, CaptureStatus, EvidenceError
from stemma.evidence.extract import (
    HTML_TYPES,
    PDF_TYPE,
    TEXT_TYPES,
    XML_TYPES,
    extract,
    extractor_versions,
    sniff_content_type,
)
from stemma.evidence.fetch import Fetcher, FetchResult
from stemma.evidence.quotes import find_quote
from stemma.evidence.render import Renderer, RenderHttpError, default_renderer
from stemma.evidence.sec import (
    EdgarToolsBackend,
    FilingRef,
    SecBackend,
    check_accession,
    check_document,
    concept_url,
    facts_from_concept_json,
    parse_concept,
)
from stemma.evidence.store import Store, check_sha256, check_text_id, new_receipt_id, utc_now
from stemma.evidence.transport import StdlibTransport, Transport

_UNSET: Any = object()
# Resource limit: 50 ms steps a writer waits for a concurrent writer's extraction record.
_EXTRACTION_WAIT_STEPS = 40


def _http_failure(status: int) -> CaptureStatus:
    if status in (404, 410):
        return CaptureStatus.NOT_FOUND
    if status in (401, 403, 407, 451):
        return CaptureStatus.FORBIDDEN
    if status == 429:
        return CaptureStatus.RATE_LIMITED
    return CaptureStatus.HTTP_ERROR


def _open_beneath(root: Path, parts: list[str]) -> int | tuple[CaptureStatus, str]:
    """Open `root/parts...` one component at a time, never following a symbolic link
    (O_NOFOLLOW on every directory and the file), so a link swapped in at any level
    cannot lead outside `root`. Returns a file descriptor or (status, detail)."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current = os.open(root, flags_dir)
    except OSError as exc:
        return CaptureStatus.FILE_NOT_FOUND, f"no task/ folder: {exc.strerror}"
    try:
        for index, name in enumerate(parts):
            last = index == len(parts) - 1
            try:
                # O_NONBLOCK on the file: opening a FIFO must not wait for a writer;
                # the regular-file check comes straight after.
                follow_free = os.O_RDONLY | os.O_NOFOLLOW | (
                    os.O_NONBLOCK if last else os.O_DIRECTORY)
                fd = os.open(name, follow_free, dir_fd=current)
            except OSError as exc:
                try:
                    is_link = stat.S_ISLNK(os.stat(name, dir_fd=current,
                                                   follow_symlinks=False).st_mode)
                except OSError:
                    is_link = False
                if is_link or exc.errno == errno.ELOOP:
                    return (CaptureStatus.PATH_OUTSIDE_TASK,
                            (f"{'/'.join(parts[:index + 1])} is a symbolic link; links are "
                            "not followed"))
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    return CaptureStatus.FILE_NOT_FOUND, "no such file under task/"
                return CaptureStatus.FILE_UNREADABLE, f"cannot open: {exc.strerror}"
            os.close(current)
            current = fd
        result, current = current, -1
        return result
    finally:
        if current != -1:
            os.close(current)


def _printable(value: object) -> str:
    """Agent-supplied text made safe to write into a UTF-8 receipt."""
    return str(value).encode("utf-8", "replace").decode("utf-8")[:2000]


def _check_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise EvidenceError("bad_date", f"{name} must be YYYY-MM-DD") from exc


class EvidenceService:
    def __init__(
        self,
        run_dir: str | Path,
        *,
        transport: Transport | None = None,
        cache_dir: str | Path | None = _UNSET,
        sec_backend: SecBackend | None = None,
        sec_identity: str | None = _UNSET,
        renderer: Renderer | None = _UNSET,
        throttle: bool = True,
    ) -> None:
        self._run_dir_given = Path(os.path.abspath(run_dir))
        self.run_dir = Path(run_dir).resolve()
        if not self.run_dir.is_dir():
            raise EvidenceError("bad_run_dir", f"{run_dir} is not a folder")
        self.store = Store(self.run_dir)
        if sec_identity is _UNSET:
            sec_identity = os.environ.get("STEMMA_SEC_IDENTITY") or None
        self.sec_identity: str | None = sec_identity
        self.fetcher = Fetcher(transport or StdlibTransport(), sec_user_agent=sec_identity,
                               throttle=throttle)
        self.archive = Archive(self.fetcher)
        if cache_dir is _UNSET:
            cache_dir = os.environ.get("STEMMA_CACHE") or None
        self.cache = RequestCache(Path(cache_dir)) if cache_dir else None
        self._sec_backend = sec_backend
        # Built only when a run enables the browser fallback and a page needs it.
        self._renderer: Any = renderer

    # Run facts --------------------------------------------------------------------

    @property
    def as_of(self) -> str | None:
        path = self.run_dir / "run.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text("utf-8")).get("as_of")
        except (OSError, ValueError) as exc:
            raise EvidenceError("bad_run_json", f"run.json unreadable: {exc}") from exc
        if value in (None, ""):
            return None
        checked = _check_date(str(value), "run.json as_of")
        assert checked is not None
        return checked

    def _require_sec_identity(self) -> None:
        """SEC tools never run without the configured identity; there is no default."""
        if not self.sec_identity or "@" not in self.sec_identity:
            raise EvidenceError(
                "sec_identity_not_configured",
                "SEC requires a contact identity; STEMMA_SEC_IDENTITY is not set",
            )

    def _sec(self) -> SecBackend:
        self._require_sec_identity()
        if self._sec_backend is None:
            # edgartools keeps its own lookup cache; keep it out of the home folder.
            base = self.cache.root if self.cache is not None else self.store.derived_dir
            self._sec_backend = EdgarToolsBackend(self.sec_identity or "", base / "edgartools")
        return self._sec_backend

    # Receipts -----------------------------------------------------------------------

    def _base_receipt(self, kind: str, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise EvidenceError("reason_required", "say why this source is being captured")
        return {
            "receipt_id": new_receipt_id(),
            "created_at": utc_now(),
            "kind": kind,
            "reason": _printable(reason.strip()),
            "as_of": self.as_of,
            "status": None,
            "detail": None,
            "sha256": None,
            "bytes_len": None,
            "content_type": None,
            "charset": None,
            "text_path": None,
            "text_chars": None,
            "outline": [],
            "outline_truncated": False,
            "pages": None,
            "flagged_ranges": [],
            "text_id": None,
            "warnings": [],
            "served_from_cache": False,
            "original_fetched_at": None,
            "extractor": None,
            "http": None,
        }

    def _finish(self, receipt: dict[str, Any], body: bytes | None, declared_type: str | None,
                *, name_hint: str = "", withhold_text: bool = False,
                loud_note: str | None = None) -> dict[str, Any]:
        """Store bytes, extract and index text, write the receipt. Returns it."""
        if body is not None:
            sha = self.store.put_bytes(body)
            receipt["sha256"] = sha
            receipt["bytes_len"] = len(body)
            if receipt["status"] is None:
                if withhold_text:
                    receipt["status"] = CaptureStatus.TEXT_WITHHELD_AS_OF
                    receipt["detail"] = "text would show information dated after as_of"
                    receipt["content_type"] = (declared_type or "").split(";")[0] or None
                else:
                    self._extract_into(receipt, sha, body, declared_type, name_hint)
        if loud_note and receipt["status"] in (CaptureStatus.EMPTY_TEXT, CaptureStatus.SHORT_TEXT):
            receipt["warnings"].append(loud_note)
        receipt["status"] = str(receipt["status"])
        self.store.put_receipt(receipt)
        return receipt

    @staticmethod
    def _variant(body: bytes, declared_type: str | None, name_hint: str,
                 form: str | None) -> tuple[str, str, str | None]:
        """What decides the stored text besides the bytes: the content type, the
        charset chosen, and (for SEC filings) the form that adds Item anchors."""
        content_type = sniff_content_type(declared_type, body, name_hint)
        label: str | None = None
        if content_type != PDF_TYPE and (content_type in HTML_TYPES or content_type in XML_TYPES
                                         or content_type in TEXT_TYPES
                                         or content_type.startswith("text/")):
            xml = content_type in XML_TYPES or content_type == "application/xhtml+xml"
            kind = "xml" if xml else ("html" if content_type in HTML_TYPES else "text")
            label = charset.choose(body, declared_type, kind)
        sec_form = form if sec_outline.applies_to(form) else None
        material = f"{content_type}\n{label}\n{sec_form}"
        return hashlib.sha256(material.encode()).hexdigest()[:12], content_type, label

    def _extract_into(self, receipt: dict[str, Any], sha: str, body: bytes,
                      declared_type: str | None, name_hint: str) -> None:
        form = (receipt.get("sec") or {}).get("form")
        variant, _, _ = self._variant(body, declared_type, name_hint, form)
        # Reuse a stored text only when the same bytes were read the same way.
        for text_id in (sha, f"{sha}~{variant}"):
            earlier = self.store.get_extraction(text_id)
            if earlier is not None and earlier.get("variant") == variant:
                self._reuse_extraction(receipt, text_id, earlier)
                return
        base_taken = self.store.get_extraction(sha) is not None
        target = f"{sha}~{variant}" if base_taken else sha
        result = extract(body, declared_type, name_hint)
        if result.text and sec_outline.applies_to(form):
            items = sec_outline.item_outline(result.text)
            if items:
                result.outline = sorted(result.outline + items, key=lambda e: int(e["start"]))
            else:
                result.warnings.append(f"sec_items_not_found: no Item headings in this {form}")
        receipt["status"] = result.status
        receipt["content_type"] = result.content_type
        receipt["charset"] = result.charset
        receipt["extractor"] = result.extractor or None
        receipt["pages"] = result.pages
        receipt["flagged_ranges"] = result.flagged
        receipt["warnings"].extend(result.warnings)
        if result.detail:
            receipt["detail"] = result.detail
        if result.text is not None and result.status in (
            CaptureStatus.OK, CaptureStatus.SHORT_TEXT, CaptureStatus.EMPTY_TEXT
        ):
            record = {
                "receipt_id": receipt["receipt_id"], "status": str(result.status),
                "content_type": result.content_type, "charset": result.charset,
                "extractor": result.extractor or None, "pages": result.pages,
                "detail": result.detail, "warnings": list(result.warnings),
                "flagged_ranges": result.flagged, "variant": variant,
            }
            for attempt in (target, f"{sha}~{variant}"):
                try:
                    self.store.put_text(attempt, result.text, result.outline, record)
                    target = attempt
                    break
                except EvidenceError as exc:
                    # A concurrent writer stored text for these bytes first.
                    if exc.code != "store_corrupt":
                        raise
                    earlier = self._await_extraction(attempt)
                    if earlier.get("variant") == variant:
                        receipt["warnings"] = [w for w in receipt["warnings"]
                                               if w not in result.warnings]
                        self._reuse_extraction(receipt, attempt, earlier)
                        return
            else:
                raise EvidenceError("store_corrupt", f"no free text slot for {sha}")
            receipt["text_id"] = target
            receipt["text_path"] = self.store.text_rel_path(target)
            receipt["text_chars"] = len(result.text)
            receipt["outline"] = result.outline[: limits.RECEIPT_OUTLINE_MAX]
            receipt["outline_truncated"] = len(result.outline) > limits.RECEIPT_OUTLINE_MAX
            if result.text:
                self.store.index_document(target, result.text, result.content_type,
                                          receipt["receipt_id"])

    def _await_extraction(self, text_id: str) -> dict[str, Any]:
        for _ in range(_EXTRACTION_WAIT_STEPS):
            earlier = self.store.get_extraction(text_id)
            if earlier is not None:
                return earlier
            time.sleep(0.05)
        raise EvidenceError("store_corrupt",
                            f"stored text {text_id} conflicts and has no record")

    def _reuse_extraction(self, receipt: dict[str, Any], text_id: str,
                          earlier: dict[str, Any]) -> None:
        """The same bytes read the same way: reuse the stored text and its record."""
        for key in ("status", "content_type", "charset", "extractor", "pages", "detail"):
            receipt[key] = earlier[key]
        receipt["flagged_ranges"] = earlier.get("flagged_ranges", [])
        receipt["warnings"].extend(earlier["warnings"])
        receipt["warnings"].append(f"text_reused_from_receipt {earlier['receipt_id']} (same bytes)")
        outline = self.store.get_outline(text_id)
        receipt["text_id"] = text_id
        receipt["text_path"] = self.store.text_rel_path(text_id)
        receipt["text_chars"] = len(self.store.get_text(text_id))
        receipt["outline"] = outline[: limits.RECEIPT_OUTLINE_MAX]
        receipt["outline_truncated"] = len(outline) > limits.RECEIPT_OUTLINE_MAX

    @staticmethod
    def _http_record(fetched: FetchResult) -> dict[str, Any]:
        return {
            "requested_url": fetched.requested_url,
            "final_url": fetched.final_url,
            "status": fetched.http_status,
            "headers": [list(pair) for pair in fetched.recorded_headers()],
            "hops": [hop.as_dict() for hop in fetched.hops],
            "elapsed_s": fetched.elapsed_s,
            "bytes_read": fetched.bytes_read,
            "content_encoding": fetched.content_encoding,
            "wire_sha256": fetched.wire_sha256,
            "limits": fetched.limits_record(),
            # Whether SEC's required contact header went out; its value is never recorded.
            "sec_identity_header_sent": fetched.sec_identity_sent,
        }

    # Fetch with the request cache ---------------------------------------------------

    def _fetch_cached(self, kind: str, url: str, receipt: dict[str, Any],
                      do_fetch: Any) -> tuple[bytes | None, str | None]:
        """Fill `receipt` from the cache or a fresh fetch. Returns (body, content type);
        body is None on failure, with `receipt["status"]` set."""
        key = request_key(kind, url, receipt["as_of"], extractor_versions())
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None and hit[0].get("_corrupt"):
                receipt["warnings"].append("cache_entry_corrupt_ignored")
            elif hit is not None:
                meta, body = hit
                receipt["served_from_cache"] = True
                receipt["original_fetched_at"] = meta.get("fetched_at")
                receipt["http"] = meta.get("http")
                if meta.get("archive"):
                    receipt["archive"] = meta["archive"]
                receipt["final_url"] = meta.get("final_url")
                return body, meta.get("content_type")
        fetched_at = utc_now()
        fetched, archive_record, failure, detail = do_fetch()
        receipt["fetched_at"] = fetched_at
        if archive_record is not None:
            receipt["archive"] = archive_record
        if fetched is not None:
            receipt["http"] = self._http_record(fetched)
            receipt["final_url"] = fetched.final_url
        if failure is not None:
            receipt["status"] = failure
            receipt["detail"] = detail
            return None, None
        assert fetched is not None
        if fetched.failure is not None:
            receipt["status"] = fetched.failure
            receipt["detail"] = fetched.detail
            return None, None
        status = fetched.http_status or 0
        content_type = fetched.header("Content-Type")
        if not 200 <= status < 300:
            if receipt["as_of"] is not None and kind == "archive":
                receipt["status"] = CaptureStatus.ARCHIVE_UNAVAILABLE
                receipt["detail"] = f"archive replay answered HTTP {status}"
            else:
                receipt["status"] = _http_failure(status)
                receipt["detail"] = f"HTTP {status}"
            if fetched.body:
                # The error page's bytes are kept (a wall says what it is), never indexed.
                sha = self.store.put_bytes(fetched.body)
                receipt["sha256"] = sha
                receipt["bytes_len"] = len(fetched.body)
                receipt["content_type"] = (content_type or "").split(";")[0] or None
            return None, None
        if self.cache is not None and fetched.body is not None:
            self.cache.put(key, {
                "fetched_at": fetched_at, "http": receipt["http"], "final_url": fetched.final_url,
                "content_type": content_type, "archive": receipt.get("archive"),
            }, fetched.body)
        return fetched.body, content_type

    # Tools ---------------------------------------------------------------------------

    def _receipted(self, kind: str, reason: str, requested: dict[str, Any],
                   work: Any) -> dict[str, Any]:
        """Run a capture; an unexpected exception still leaves a receipt, with status
        `internal_error` naming it. Typed errors (EvidenceError) pass through."""
        try:
            result: dict[str, Any] = work()
            return result
        except EvidenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded on a receipt, never swallowed
            receipt = self._base_receipt(kind, reason)
            receipt.update({k: _printable(v) for k, v in requested.items()})
            receipt["status"] = CaptureStatus.INTERNAL_ERROR
            receipt["detail"] = _printable(f"{type(exc).__name__}: {exc}")
            return self._finish(receipt, None, None)

    def capture(self, url: str, reason: str) -> dict[str, Any]:
        """Capture a web page or document; in `as_of` mode, from an archived copy."""
        self._base_receipt("web", reason)  # a missing reason is refused before any work
        return self._receipted("web", reason, {"requested_url": url},
                               lambda: self._capture(url, reason))

    def _capture(self, url: str, reason: str) -> dict[str, Any]:
        as_of = self.as_of
        kind = "archive" if as_of else "web"
        receipt = self._base_receipt(kind, reason)
        receipt["requested_url"] = _printable(url)

        def do_fetch() -> tuple[FetchResult | None, dict[str, Any] | None,
                                CaptureStatus | None, str | None]:
            if as_of:
                outcome = self.archive.capture(url, as_of)
                return outcome.fetch, outcome.record, outcome.status, outcome.detail
            return self.fetcher.fetch(url), None, None, None

        body, content_type = self._fetch_cached(kind, url, receipt, do_fetch)
        markup_like = False
        note: str | None = None
        renderer: Renderer | None = None
        if body is not None:
            media = (content_type or "").split(";")[0].strip().lower()
            markup_like = media in HTML_TYPES or media in XML_TYPES or media == ""
            if markup_like and as_of:
                note = "browser_fallback_not_used_in_as_of_mode"
            elif markup_like and not self.browser_fallback_enabled:
                note = "browser_fallback_off_for_this_run"
            elif markup_like:
                renderer = self._get_renderer()
                if renderer is None:
                    note = "browser_fallback_not_installed"
        plain = self._finish(receipt, body, content_type, loud_note=note)
        needs_fallback = plain["status"] in (CaptureStatus.EMPTY_TEXT, CaptureStatus.SHORT_TEXT)
        if needs_fallback and renderer is not None:
            return self._render_fallback(renderer, plain, reason)
        return plain

    @property
    def browser_fallback_enabled(self) -> bool:
        """A recorded run setting, off unless `run.json` turns it on."""
        path = self.run_dir / "run.json"
        if not path.exists():
            return False
        try:
            settings = json.loads(path.read_text("utf-8")).get("evidence") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(settings, dict) and settings.get("browser_fallback") is True

    def _get_renderer(self) -> Renderer | None:
        if self._renderer is _UNSET:
            self._renderer = default_renderer()
        renderer: Renderer | None = self._renderer
        return renderer

    def _render_fallback(self, renderer: Renderer, plain: dict[str, Any],
                         reason: str) -> dict[str, Any]:
        receipt = self._base_receipt("rendered", reason)
        receipt["requested_url"] = plain.get("final_url") or plain["requested_url"]
        receipt["fallback_of"] = plain["receipt_id"]
        receipt["renderer"] = {"name": renderer.name, "version": renderer.version}
        receipt["fetched_at"] = utc_now()
        try:
            rendered = renderer.render(receipt["requested_url"])
        except Exception as exc:  # noqa: BLE001 - recorded on a receipt, never swallowed
            if isinstance(exc, RenderHttpError):
                receipt["status"] = _http_failure(exc.status)
                receipt["detail"] = f"browser fallback: main document answered HTTP {exc.status}"
            else:
                receipt["status"] = CaptureStatus.TRANSPORT_ERROR
                receipt["detail"] = f"browser fallback failed: {exc}"
            failed = self._finish(receipt, None, None)
            plain = dict(plain)
            plain["warnings"] = plain["warnings"] + [
                f"browser_fallback_failed: receipt {failed['receipt_id']}"
            ]
            return plain
        receipt["final_url"] = rendered.final_url
        if rendered.blocked_requests:
            receipt["warnings"].append(
                f"browser_requests_refused: {len(rendered.blocked_requests)}")
            receipt["blocked_requests"] = rendered.blocked_requests[:50]
        return self._finish(receipt, rendered.html, "text/html; charset=utf-8")

    def capture_file(self, path: str, reason: str) -> dict[str, Any]:
        """Capture one of the user's attachments, which live under the run's `task/`."""
        self._base_receipt("file", reason)
        return self._receipted("file", reason, {"requested_path": path},
                               lambda: self._capture_file(path, reason))

    def _capture_file(self, path: str, reason: str) -> dict[str, Any]:
        receipt = self._base_receipt("file", reason)
        receipt["requested_path"] = _printable(path)

        def refuse(status: CaptureStatus, detail: str) -> dict[str, Any]:
            receipt["status"] = status
            receipt["detail"] = detail
            return self._finish(receipt, None, None)

        try:
            parts = self._task_relative_parts(path)
        except ValueError as exc:
            return refuse(CaptureStatus.PATH_OUTSIDE_TASK, str(exc))
        opened = _open_beneath(self.run_dir / "task", parts)
        if isinstance(opened, tuple):
            return refuse(*opened)
        with os.fdopen(opened, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return refuse(CaptureStatus.FILE_NOT_FOUND, "not a regular file under task/")
            if info.st_nlink > 1:
                # A hard link can name a file whose other name is outside task/.
                return refuse(CaptureStatus.PATH_OUTSIDE_TASK,
                              "file has other hard links; copy it into task/ instead")
            if info.st_size > limits.MAX_BODY_BYTES:
                return refuse(CaptureStatus.TOO_LARGE,
                              f"{info.st_size} bytes over limit {limits.MAX_BODY_BYTES}")
            try:
                body = handle.read(limits.MAX_BODY_BYTES + 1)
            except OSError as exc:
                return refuse(CaptureStatus.FILE_UNREADABLE, f"cannot read: {exc.strerror}")
        if len(body) > limits.MAX_BODY_BYTES:
            return refuse(CaptureStatus.TOO_LARGE, f"over limit {limits.MAX_BODY_BYTES}")
        # The path actually opened, component by component, with no link followed.
        receipt["source_path"] = "/".join(["task", *parts])
        receipt["fetched_at"] = utc_now()
        return self._finish(receipt, body, None, name_hint=parts[-1])

    def _task_relative_parts(self, path: str) -> list[str]:
        """The components of `path` below `task/`, judged by spelling alone (no
        link is resolved here; `_open_beneath` refuses links while opening)."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("path is empty or holds a NUL character")
        try:
            path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("path is not valid Unicode text") from exc
        given = PurePosixPath(path)
        if given.is_absolute():
            for root in (self.run_dir, self._run_dir_given):
                with contextlib.suppress(ValueError):
                    given = PurePosixPath(given.relative_to(PurePosixPath(root)))
                    break
            else:
                raise ValueError("attachments are read only from the run's task/ folder")
        parts = [p for p in given.parts if p not in ("", ".")]
        if not parts or parts[0] != "task" or len(parts) < 2 or ".." in parts:
            raise ValueError("attachments are read only from the run's task/ folder")
        return parts[1:]

    # SEC -----------------------------------------------------------------------------

    def _filed_before(self, filed_before: str | None, warnings: list[str]) -> str | None:
        filed_before = _check_date(filed_before, "filed_before")
        as_of = self.as_of
        if as_of is None:
            return filed_before
        if filed_before is None:
            return as_of
        if filed_before > as_of:
            warnings.append(f"filed_before {filed_before} is after as_of; using as_of {as_of}")
            return as_of
        return filed_before

    def _registry_path(self, accession: str) -> Path:
        return self.store.derived_dir / "sec-filings" / f"{accession}.json"

    def _remember(self, ref: FilingRef) -> None:
        path = self._registry_path(ref.accession)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_text(json.dumps(ref.as_dict()), "utf-8")
            os.replace(tmp, path)

    def _filing_ref(self, accession: str) -> FilingRef:
        path = self._registry_path(accession)
        if path.exists():
            return FilingRef(**json.loads(path.read_text("utf-8")))
        ref = self._sec().lookup(accession)
        self._remember(ref)
        return ref

    def _resolve_company(self, backend: SecBackend, company: str,
                         warnings: list[str]) -> Any:
        """Resolve a company. A ticker or name goes through today's SEC mapping; in an
        `as_of` run that mapping may not be what it was then, so it is warned."""
        ref = backend.resolve(company)
        is_cik = str(company).strip().isascii() and str(company).strip().isdigit()
        if self.as_of is not None and not is_cik:
            warnings.append(
                f"{company!r} was resolved to CIK {ref.cik} ({ref.name}) with today's ticker "
                "and name mapping, which may differ from the as_of date; pass the CIK to be "
                "sure")
        return ref

    def sec_filings(self, company: str, form: str | None = None, filed_after: str | None = None,
                    filed_before: str | None = None) -> dict[str, Any]:
        """Filings with accession numbers and filing dates, newest first.
        Bounds are strict: filed after `filed_after` and before `filed_before`."""
        warnings: list[str] = []
        after = _check_date(filed_after, "filed_after")
        before = self._filed_before(filed_before, warnings)
        backend = self._sec()
        ref = self._resolve_company(backend, company, warnings)
        rows = [f for f in backend.filings(ref.cik, form)
                if (before is None or f.filing_date < before)
                and (after is None or f.filing_date > after)]
        rows.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        truncated = len(rows) > limits.SEC_FILINGS_MAX
        rows = rows[: limits.SEC_FILINGS_MAX]
        for row in rows:
            self._remember(row)
        return {"company": ref.name, "cik": ref.cik, "form": form, "filed_after": after,
                "filed_before": before, "filings": [r.as_dict() for r in rows],
                "truncated": truncated, "warnings": warnings}

    def sec_capture(self, accession: str, document: str | None = None,
                    reason: str = "SEC filing") -> dict[str, Any]:
        """Capture one filing's document (its primary document by default)."""
        check_accession(accession)
        if document is not None:
            check_document(document)
        self._require_sec_identity()
        ref = self._filing_ref(accession)
        return self._receipted("sec", reason, {"requested_accession": accession},
                               lambda: self._sec_capture(ref, document, reason))

    def _sec_capture(self, ref: FilingRef, document: str | None, reason: str) -> dict[str, Any]:
        receipt = self._base_receipt("sec", reason)
        receipt["sec"] = dict(ref.as_dict(), document=document or ref.primary_document)
        as_of = receipt["as_of"]
        if as_of is not None and ref.filing_date >= as_of:
            receipt["status"] = CaptureStatus.FILED_AFTER_AS_OF
            receipt["detail"] = f"filed {ref.filing_date}, not before as_of {as_of}"
            return self._finish(receipt, None, None)
        url = ref.document_url(document)
        receipt["requested_url"] = url

        def do_fetch() -> tuple[FetchResult | None, None, None, None]:
            return self.fetcher.fetch(url), None, None, None

        body, content_type = self._fetch_cached("sec", url, receipt, do_fetch)
        return self._finish(receipt, body, content_type, name_hint=url.rsplit("/", 1)[-1])

    def sec_facts(self, company: str, concept: str, filed_before: str | None = None,
                  form: str | None = None) -> dict[str, Any]:
        """As-filed XBRL facts for one concept, each with its filing date and the
        receipt of the filing it came from. Never collapsed to the latest figure."""
        warnings: list[str] = []
        before = self._filed_before(filed_before, warnings)
        taxonomy, tag = parse_concept(concept)
        backend = self._sec()
        ref = self._resolve_company(backend, company, warnings)
        url = concept_url(ref.cik, taxonomy, tag)
        as_of = self.as_of
        receipt = self._base_receipt("sec_api", f"XBRL facts for {taxonomy}:{tag}")
        receipt["requested_url"] = url
        receipt["sec"] = {"cik": ref.cik, "company": ref.name, "concept": f"{taxonomy}:{tag}"}

        def do_fetch() -> tuple[FetchResult | None, None, None, None]:
            return self.fetcher.fetch(url), None, None, None

        body, content_type = self._fetch_cached("sec", url, receipt, do_fetch)
        source = self._finish(receipt, body, content_type, withhold_text=as_of is not None)
        if body is None:
            raise EvidenceError(
                "sec_facts_unavailable",
                f"{url}: {source['status']} ({source['detail']}); receipt {source['receipt_id']}",
            )
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise EvidenceError("sec_facts_unavailable", f"answer was not JSON: {exc}") from exc
        def form_matches(fact_form: object) -> bool:
            # `form="10-K"` keeps amendments too ("10-K/A"), labelled by their own form.
            return form is None or str(fact_form).upper() in (form.upper(), f"{form.upper()}/A")

        facts = [f for f in facts_from_concept_json(data)
                 if (before is None or str(f["filed"]) < before) and form_matches(f["form"])]
        amended = sum(1 for f in facts if str(f["form"]).upper().endswith("/A"))
        if form is not None and amended:
            warnings.append(f"{amended} facts come from amendments ({form}/A), labelled as such")
        if before is not None:
            # SEC re-assigns `frame` to the latest filing for a period, so it can
            # point at a filing after the cutoff. It is dropped from dated answers.
            for fact in facts:
                fact.pop("frame", None)
        truncated = len(facts) > limits.SEC_FACTS_MAX
        facts = facts[-limits.SEC_FACTS_MAX:]
        known = {f.accession: f for f in backend.filings(ref.cik, None)}
        order = sorted({str(f["accession"]) for f in facts},
                       key=lambda a: (known[a].filing_date if a in known else "", a),
                       reverse=True)
        filing_receipts: dict[str, dict[str, Any]] = {}
        for index, accession in enumerate(order):
            if index >= limits.SEC_FACTS_MAX_FILINGS:
                filing_receipts[accession] = {"receipt_id": None,
                                              "status": "not_captured_limit_reached"}
                continue
            if accession not in known:
                filing_receipts[accession] = {"receipt_id": None,
                                              "status": "filing_not_in_listing"}
                continue
            self._remember(known[accession])
            captured = self.sec_capture(accession, None,
                                        reason=f"filing behind {taxonomy}:{tag} facts")
            filing_receipts[accession] = {"receipt_id": captured["receipt_id"],
                                          "status": captured["status"]}
        skipped = [a for a, r in filing_receipts.items() if r["receipt_id"] is None]
        if skipped:
            warnings.append(f"{len(skipped)} filings not captured (limit "
                            f"{limits.SEC_FACTS_MAX_FILINGS} or not listed); "
                            "use sec_capture on their accessions")
        for fact in facts:
            entry = filing_receipts.get(str(fact["accession"]), {})
            fact["filing_receipt_id"] = entry.get("receipt_id")
        return {"company": ref.name, "cik": ref.cik, "concept": f"{taxonomy}:{tag}",
                "filed_before": before, "form": form, "source_receipt_id": source["receipt_id"],
                "facts": facts, "filings": filing_receipts, "truncated": truncated,
                "warnings": warnings}

    # Reading in parts ------------------------------------------------------------------

    def outline(self, sha256: str) -> dict[str, Any]:
        check_text_id(sha256)
        outline = self.store.get_outline(sha256)
        text = self.store.get_text(sha256)
        return {"sha256": sha256, "text_chars": len(text),
                "outline": outline[: limits.OUTLINE_MAX],
                "truncated": len(outline) > limits.OUTLINE_MAX}

    def read(self, sha256: str, start: int = 0, length: int = limits.READ_MAX_CHARS) -> dict[str, Any]:
        check_text_id(sha256)
        text = self.store.get_text(sha256)
        if not isinstance(start, int) or start < 0 or start > len(text):
            raise EvidenceError("bad_range", f"start must be between 0 and {len(text)}")
        if not isinstance(length, int) or length <= 0:
            raise EvidenceError("bad_range", "length must be a positive number")
        length = min(length, limits.READ_MAX_CHARS)
        end = min(start + length, len(text))
        return {"sha256": sha256, "start": start, "end": end, "text_chars": len(text),
                "text": text[start:end], "next_start": end if end < len(text) else None}

    def read_section(self, sha256: str, anchor: str) -> dict[str, Any]:
        check_text_id(sha256)
        for item in self.store.get_outline(sha256):
            if item["anchor"] == anchor:
                text = self.store.get_text(sha256)
                section_end = int(item["end"])
                end = min(section_end, int(item["start"]) + limits.READ_MAX_CHARS)
                return {"sha256": sha256, "anchor": anchor, "title": item["title"],
                        "start": item["start"], "end": end, "section_end": section_end,
                        "text_chars": len(text), "text": text[int(item["start"]):end],
                        "next_start": end if end < section_end else None}
        raise EvidenceError("unknown_anchor", f"{anchor!r} is not in this document's outline")

    # Search, quotes, tables ------------------------------------------------------------

    def search_store(self, query: str, mode: str = "words", sha256: str | None = None,
                     limit: int = 10) -> dict[str, Any]:
        if sha256 is not None:
            check_text_id(sha256)
        if not isinstance(query, str) or not query.strip():
            raise EvidenceError("bad_query", "the query is empty")
        if "\x00" in query:
            raise EvidenceError("bad_query", "the query holds a NUL character")
        try:
            query.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise EvidenceError("bad_query", "the query is not valid Unicode text") from exc
        if not isinstance(limit, int) or limit <= 0:
            raise EvidenceError("bad_limit", "limit must be a positive number")
        limit = min(limit, limits.SEARCH_MAX_HITS)
        if mode == "words":
            hits = self.store.search_words(query, sha256, limit)
        elif mode == "exact":
            hits = self.store.search_exact(query, sha256, limit)
        else:
            raise EvidenceError("bad_mode", "mode is 'words' or 'exact'")
        for hit in hits:
            hit["receipt_id"] = self.store.first_receipt_for(hit["sha256"])
        return {"query": query, "mode": mode, "hits": hits}

    def check_quote(self, receipt_id: str, quote: str) -> dict[str, Any]:
        receipt = self.store.get_receipt(receipt_id)
        if receipt["status"] not in HAS_TEXT or not receipt.get("sha256"):
            raise EvidenceError("no_text", f"receipt {receipt_id} has status "
                                f"{receipt['status']} and no quotable text")
        result = find_quote(self.store.get_text(receipt.get("text_id") or receipt["sha256"]), quote,
                            receipt.get("flagged_ranges"))
        return dict(result, receipt_id=receipt_id, sha256=receipt["sha256"])

    def read_text(self, receipt_id: str) -> str:
        receipt = self.store.get_receipt(receipt_id)
        if receipt["status"] not in HAS_TEXT or not receipt.get("sha256"):
            raise EvidenceError("no_text", f"receipt {receipt_id} has status "
                                f"{receipt['status']} and no stored text")
        return self.store.get_text(receipt.get("text_id") or receipt["sha256"])

    def tables(self, sha256: str) -> dict[str, Any]:
        from stemma.evidence import tables as docling_tables

        check_sha256(sha256)
        if not self.store.bytes_path(sha256).exists():
            raise EvidenceError("unknown_sha256", f"no stored bytes {sha256}")
        version = docling_tables.docling_version()
        if version is None:
            return {"sha256": sha256, "status": "not_installed",
                    "detail": "Docling is not installed (optional extra evidence-tables)"}
        cached = self.store.derived_dir / f"{sha256}.tables.json"
        if cached.exists():
            loaded = json.loads(cached.read_text("utf-8"))
            found: list[dict[str, Any]] = loaded["tables"]
        else:
            body = self.store.get_bytes(sha256)
            from stemma.evidence.extract import sniff_content_type

            content_type = sniff_content_type(None, body)
            try:
                found = docling_tables.convert_tables(body, content_type)
            except Exception as exc:  # noqa: BLE001 - returned as a typed status
                return {"sha256": sha256, "status": "extraction_failed",
                        "detail": f"docling: {exc}", "docling": version}
            tmp = cached.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_bytes(docling_tables.dumps(found, version))
            os.replace(tmp, cached)
        kept, truncated = docling_tables.bounded(found)
        return {"sha256": sha256, "status": "ok", "docling": version,
                "note": docling_tables.DERIVED_NOTE, "tables": kept,
                "table_count": len(found), "truncated": truncated}
