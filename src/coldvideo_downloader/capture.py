#!/usr/bin/env python3
"""Capture decrypted fMP4 from the real player using CDP function breakpoints.

Page functions are left intact. Parts are persisted atomically and bound to the
track URL. Only complete, validated audio is published to the requested output.
"""
import argparse
import asyncio
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from patchright.async_api import Error as BrowserError, async_playwright

HEAD_JS = ("function(){const u=this.buffer?new Uint8Array(this.buffer,this.byteOffset||0,this.byteLength)"
           ":new Uint8Array(this);return {len:u.length,head:btoa(String.fromCharCode.apply(null,u.subarray(0,32)))};}")
FULL_JS = ("function(){const u=this.buffer?new Uint8Array(this.buffer,this.byteOffset||0,this.byteLength)"
           ":new Uint8Array(this);let s='';const C=0x8000;for(let i=0;i<u.length;i+=C)"
           "s+=String.fromCharCode.apply(null,u.subarray(i,i+C));return btoa(s);}")
ENC_JS = ("function(){const u=new Uint8Array(this.byteLength);this.copyTo(u);let s='';const C=0x8000;"
          "for(let i=0;i<u.length;i+=C)s+=String.fromCharCode.apply(null,u.subarray(i,i+C));return btoa(s);}")
MIN_LEN, MAX_LEN = 8, 8_000_000
SINKS = {
    "SourceBuffer": "typeof SourceBuffer!=='undefined'?SourceBuffer.prototype.appendBuffer:null",
    "ManagedSourceBuffer": "typeof ManagedSourceBuffer!=='undefined'?ManagedSourceBuffer.prototype.appendBuffer:null",
    "AudioDecoder": "typeof AudioDecoder!=='undefined'?AudioDecoder.prototype.decode:null",
    "BaseAudioContext": "typeof BaseAudioContext!=='undefined'?BaseAudioContext.prototype.decodeAudioData:null",
    "MediaSource": "typeof MediaSource!=='undefined'?MediaSource.prototype.addSourceBuffer:null",
    "ManagedMediaSource": "typeof ManagedMediaSource!=='undefined'?ManagedMediaSource.prototype.addSourceBuffer:null",
    "HTMLMediaElement": "HTMLMediaElement.prototype.play",
}
MEDIA_SINKS = {"SourceBuffer", "ManagedSourceBuffer", "AudioDecoder", "BaseAudioContext"}
RATE_JS = """function(requested, expected, total) {
    let ahead = 0, end = 0;
    for (let i = 0; i < this.buffered.length; i++) {
        if (this.buffered.start(i) <= this.currentTime && this.currentTime < this.buffered.end(i)) {
            end = this.buffered.end(i);
            ahead = end - this.currentTime;
            break;
        }
    }
    const complete = total > 0 && end >= total;
    const low = Math.max(10, requested * 6), high = Math.max(20, requested * 12);
    let fallback = null, slowdown = null, desired = this.playbackRate;
    if (!this.ended) {
        if (this.playbackRate !== expected) fallback = 'player changed playback rate';
        else if (this.paused || this.readyState < 3) {
            desired = 1; slowdown = 'playback waiting';
        } else if (this.playbackRate > 1 && !complete && ahead < low) {
            desired = 1; slowdown = 'low playback buffer';
        } else if (this.playbackRate === 1) {
            if (ahead >= high || complete) desired = requested;
            else slowdown = 'buffer refilling';
        }
    }
    if (fallback) desired = 1;
    if (desired !== this.playbackRate) this.playbackRate = desired;
    if (this.playbackRate !== desired) fallback = 'player rejected playback rate';
    if (fallback && this.playbackRate !== 1) this.playbackRate = 1;
    return {actual: this.playbackRate, buffer_seconds: ahead,
            slowdown_reason: slowdown, fallback_reason: fallback};
}"""


class CaptureError(Exception):
    pass


def is_connection_error(exc):
    return isinstance(exc, BrowserError) and any(message in str(exc).lower() for message in (
        "target page, context or browser has been closed", "session closed",
        "connection closed", "browser has been closed", "page crashed"))


@contextmanager
def staged_path(path):
    """Use a sibling staging file; never truncate a published artifact."""
    fd, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def publish(temporary, path):
    with temporary.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    with staged_path(path) as temporary:
        temporary.write_bytes(data)
        publish(temporary, path)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2).encode())


@contextmanager
def parts_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaptureError("another capture is using this output path") from exc
        yield


def boxes(data):
    """Walk ISO BMFF boxes, including extended sizes and final size-zero boxes."""
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise CaptureError("truncated MP4 box header")
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise CaptureError("truncated extended MP4 box header")
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise CaptureError("invalid MP4 box size")
        yield kind, data[offset + header:offset + size]
        offset += size


def classify(head):
    if head[4:8] == b"ftyp":
        return "init"
    if head[4:8] in (b"moof", b"styp", b"sidx", b"emsg"):
        return "frag"
    return None


def mfhd_seq(data):
    seqs = []
    for kind, payload in boxes(data):
        if kind == b"moof":
            for child, body in boxes(payload):
                if child == b"mfhd" and len(body) == 8:
                    seqs.append(int.from_bytes(body[4:8], "big"))
    if len(seqs) != 1:
        raise CaptureError("expected exactly one mfhd sequence number per fragment")
    return seqs[0]


class Store:
    def __init__(self, parts_dir, url):
        self.dir = parts_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        manifest = self.dir / "manifest.json"
        identity = {"version": 1, "url": url.split("#", 1)[0]}
        if manifest.exists():
            if json.loads(manifest.read_text()) != identity:
                raise CaptureError("parts belong to another track or format; choose a different output path")
        elif any(self.dir.glob("*.bin")):
            raise CaptureError("existing parts have no track identity; choose a different output path")
        else:
            write_json(manifest, identity)
        self.init = None
        self.frags = {}
        self.enc = []
        self.seen = set()
        self.load()

    def load(self):
        init_path = self.dir / "init.bin"
        if init_path.exists():
            self.init = init_path.read_bytes()
            self.validate_init(self.init)
            self.seen.add(hashlib.sha256(self.init).digest())
        for path in sorted(self.dir.glob("frag_*.bin")):
            data = path.read_bytes()
            seq = mfhd_seq(data)
            if path.stem != f"frag_{seq:010d}":
                raise CaptureError(f"fragment filename disagrees with its sequence: {path.name}")
            self.frags[seq] = path
            self.seen.add(hashlib.sha256(data).digest())
        for path in sorted(self.dir.glob("enc_*.bin")):
            self.enc.append(path)
            self.seen.add(hashlib.sha256(path.read_bytes()).digest())
        if self.frags:
            print(f"loaded {len(self.frags)} persisted fragments", flush=True)

    @staticmethod
    def validate_init(data):
        kinds = {kind for kind, _ in boxes(data)}
        if not {b"ftyp", b"moov"} <= kinds:
            raise CaptureError("init is missing ftyp/moov")

    def add(self, kind, data):
        digest = hashlib.sha256(data).digest()
        if digest in self.seen:
            return False
        if kind == "init":
            self.validate_init(data)
            if self.init is not None:
                raise CaptureError("player init changed; refusing to mix different streams")
            atomic_write(self.dir / "init.bin", data)
            self.init = data
            print(f"  init {len(data)}B", flush=True)
        else:
            seq = mfhd_seq(data)
            if seq in self.frags:
                raise CaptureError(f"conflicting data for fragment {seq}")
            path = self.dir / f"frag_{seq:010d}.bin"
            atomic_write(path, data)
            self.frags[seq] = path
            if len(self.frags) <= 4 or len(self.frags) % 10 == 0:
                print(f"  frag seq={seq} {len(data)}B total={len(self.frags)}", flush=True)
        self.seen.add(digest)
        return True

    def add_enc(self, data):
        digest = hashlib.sha256(data).digest()
        if digest in self.seen:
            return False
        path = self.dir / f"enc_{len(self.enc):010d}.bin"
        atomic_write(path, data)
        self.enc.append(path)
        self.seen.add(digest)
        return True

    def assemble(self, out):
        seqs = sorted(self.frags)
        with staged_path(out) as temporary:
            with temporary.open("wb") as fh:
                if self.init:
                    fh.write(self.init)
                for seq in seqs:
                    fh.write(self.frags[seq].read_bytes())
            publish(temporary, out)
        gaps = []
        for left, right in zip(seqs, seqs[1:]):
            if right > left + 1:
                gaps.append([left + 1, right - 1])
        return {"init": self.init is not None, "frags": len(seqs),
                "range": [seqs[0], seqs[-1]] if seqs else None, "gaps": gaps}


def run_media(cmd, timeout):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode or result.stderr.strip():
        raise CaptureError(result.stderr.strip()[:500] or f"media tool exited {result.returncode}")
    return result.stdout


def probe_media(path, expected_duration=None, decode=True):
    if not shutil.which("ffprobe"):
        raise CaptureError("ffprobe is required to validate timeline coverage")
    command = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
               "-show_entries", "stream=nb_read_frames:packet=pts_time,duration_time", "-of", "json"]
    if decode:
        command.append("-count_frames")
    report = json.loads(run_media(command + [str(path)], 180))
    if not report.get("streams") or not report.get("packets"):
        raise CaptureError("no audio stream or packets")
    frames = int(report["streams"][0].get("nb_read_frames", 0)) if decode else None
    if decode and frames <= 0:
        raise CaptureError("no decoded audio frames")
    if expected_duration is not None and expected_duration <= 0:
        raise CaptureError("track duration is unknown")
    spans = sorted((float(packet["pts_time"]), float(packet["duration_time"]))
                   for packet in report["packets"])
    if any(not math.isfinite(t) or not math.isfinite(d) or d <= 0 for t, d in spans):
        raise CaptureError("invalid packet timestamps or durations")
    beginning = spans[0][0]
    end = beginning
    for timestamp, duration in spans:
        if timestamp > end + 0.005:
            raise CaptureError(f"audio timeline gap at {end:.3f}s -> {timestamp:.3f}s")
        end = max(end, timestamp + duration)
    if abs(beginning) > 0.1:
        raise CaptureError(f"audio starts at {beginning:.3f}s instead of zero")
    if expected_duration is not None and abs(end - expected_duration) > 1.25:
        raise CaptureError(f"audio covers {beginning:.3f}s..{end:.3f}s, expected 0..{expected_duration}s")
    if decode:
        if not shutil.which("ffmpeg"):
            raise CaptureError("ffmpeg is required for strict decode verification")
        run_media(["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
                   "-map", "0:a:0", "-f", "null", "-"], 300)
    return {"frames": frames, "start": beginning, "end": end, "decoded": decode}


def remux(src, dst):
    if not shutil.which("ffmpeg"):
        raise CaptureError("ffmpeg is required to remux; use --no-fix to keep fMP4")
    run_media(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:a:0",
               "-c", "copy", "-movflags", "+faststart", str(dst)], 300)


async def send(cdp, method, params=None):
    return await asyncio.wait_for(cdp.send(method, params or {}), timeout=10)


class SinkTracker:
    """Keep breakpoints on both current functions and previously cached natives."""
    def __init__(self, cdp, report_error, host=""):
        self.cdp = cdp
        self.report_error = report_error
        self.host = (host or "").lower()
        self.contexts = {}
        self.active = {}
        self.lock = asyncio.Lock()

    def created(self, event):
        context = event.get("context", {})
        host = (urlsplit(context.get("origin", "")).hostname or "").lower()
        if context.get("auxData", {}).get("isDefault") and (
                not self.host or host == self.host or host.endswith("." + self.host)):
            cid = context["id"]
            self.contexts[cid] = {}
            self.active[cid] = set()
            return cid
        return None

    def destroyed(self, event):
        cid = event.get("executionContextId")
        self.contexts.pop(cid, None)
        self.active.pop(cid, None)

    def cleared(self, _event):
        self.contexts.clear()
        self.active.clear()

    async def arm(self, cid, force=False):
        async with self.lock:
            records = self.contexts.get(cid)
            if records is None:
                return False
            active = set()
            for name, expression in SINKS.items():
                oid = None
                keep = False
                try:
                    result = await send(self.cdp, "Runtime.evaluate", {
                        "expression": expression, "contextId": cid, "returnByValue": False})
                    function = result.get("result", {})
                    oid = function.get("objectId")
                    if function.get("type") != "function" or not oid:
                        continue
                    matched = None
                    for record in records.get(name, []):
                        comparison = await send(self.cdp, "Runtime.callFunctionOn", {
                            "objectId": record["objectId"], "functionDeclaration": "function(other){return this===other;}",
                            "arguments": [{"objectId": oid}], "returnByValue": True})
                        if comparison.get("result", {}).get("value") is True:
                            matched = record
                            keep = oid == record["objectId"]
                            break
                    if matched is None:
                        breakpoint = await send(self.cdp, "Debugger.setBreakpointOnFunctionCall", {"objectId": oid})
                        records.setdefault(name, []).append({"objectId": oid, "breakpointId": breakpoint["breakpointId"]})
                        keep = True
                        if len(records[name]) > 1:
                            print(f"watchdog: re-armed replacement {name} in context {cid}", flush=True)
                    elif force or matched["breakpointId"] is None:
                        if matched["breakpointId"]:
                            await send(self.cdp, "Debugger.removeBreakpoint", {"breakpointId": matched["breakpointId"]})
                            matched["breakpointId"] = None
                        breakpoint = await send(self.cdp, "Debugger.setBreakpointOnFunctionCall", {"objectId": matched["objectId"]})
                        matched["breakpointId"] = breakpoint["breakpointId"]
                    active.add(name)
                except Exception as exc:
                    self.report_error(f"arm {name}", exc)
                finally:
                    if oid and not keep:
                        try:
                            await send(self.cdp, "Runtime.releaseObject", {"objectId": oid})
                        except Exception:
                            pass  # Object handles vanish when their realm is destroyed.
            if self.contexts.get(cid) is records:
                self.active[cid] = active
            return bool(active & MEDIA_SINKS)

    async def arm_all(self, force=False):
        results = [await self.arm(cid, force) for cid in list(self.contexts)]
        return any(results)

    async def watchdog(self):
        while True:
            await asyncio.sleep(4)
            await self.arm_all()

    def status(self):
        return {str(cid): sorted(names) for cid, names in self.active.items()}


class PlaybackRate:
    def __init__(self, requested, fallback_reason=None):
        self.media = None
        self.status = {"requested": requested, "actual": 1.0, "buffer_seconds": None,
                       "accelerated": False, "speedups": 0, "slowdowns": 0,
                       "slowdown_reason": None, "fallback_reason": fallback_reason}

    async def find_media(self, cdp, contexts):
        candidates = []
        try:
            for cid in list(contexts):
                prototype = await send(cdp, "Runtime.evaluate", {
                    "expression": "HTMLMediaElement.prototype", "contextId": cid,
                    "objectGroup": "rate-discovery"})
                objects = await send(cdp, "Runtime.queryObjects", {
                    "prototypeObjectId": prototype["result"]["objectId"], "objectGroup": "rate-discovery"})
                active = await send(cdp, "Runtime.callFunctionOn", {
                    "objectId": objects["objects"]["objectId"], "objectGroup": "rate-discovery",
                    "functionDeclaration": """function(){return this.filter(element=>{
                        // Heap queries include derived prototypes with invalid media getters.
                        try{return !element.paused && !element.ended;}catch{return false;}
                    });}"""})
                properties = await send(cdp, "Runtime.getProperties", {
                    "objectId": active["result"]["objectId"], "ownProperties": True})
                candidates.extend(prop["value"]["objectId"] for prop in properties["result"]
                                  if prop["name"].isdigit())
            if len(candidates) != 1:
                return None
            retained = await send(cdp, "Runtime.callFunctionOn", {
                "objectId": candidates[0], "functionDeclaration": "function(){return this;}",
                "objectGroup": "rate-control"})
            return retained["result"]["objectId"]
        finally:
            await send(cdp, "Runtime.releaseObjectGroup", {"objectGroup": "rate-discovery"})

    async def poll(self, cdp, contexts, total):
        if self.status["requested"] == 1 or self.status["fallback_reason"]:
            return
        if self.media is None:
            self.media = await self.find_media(cdp, contexts)
            if self.media is None:
                return
        result = await send(cdp, "Runtime.callFunctionOn", {
            "objectId": self.media, "functionDeclaration": RATE_JS, "returnByValue": True,
            "arguments": [{"value": self.status["requested"]}, {"value": self.status["actual"]},
                          {"value": total}]})
        if "exceptionDetails" in result:
            raise CaptureError("player rejected playback rate control")
        before = self.status["actual"]
        self.status.update(result["result"]["value"])
        self.status["accelerated"] = self.status["accelerated"] or self.status["actual"] > 1
        self.status["speedups"] += self.status["actual"] > before
        self.status["slowdowns"] += self.status["actual"] < before
        if self.status["actual"] != 1 and (self.status["slowdown_reason"] or self.status["fallback_reason"]):
            raise CaptureError("could not restore 1x playback")
        if self.status["fallback_reason"]:
            print(f"playback rate: 1x ({self.status['fallback_reason']}); acceleration disabled", flush=True)
        elif self.status["actual"] != before:
            reason = self.status["slowdown_reason"] or "buffer ready"
            print(f"playback rate: {self.status['actual']:g}x ({reason}), "
                  f"{self.status['buffer_seconds']:.1f}s buffered", flush=True)


class Session:
    def __init__(self, args, store, elapsed_playback=0, reconnect=False, rate_fallback=None):
        self.args = args
        self.store = store
        self.sinks = {}
        self.errors = {}
        self.error_samples = []
        self.fatal = None
        self.last_seconds = -1
        self.duration = -1
        self.last_media = 0
        self.media_events = 0
        self.tasks = set()
        self.closing = False
        self.tracker = None
        self.armed_sinks = {}
        self.elapsed_playback = elapsed_playback
        self.playback_seconds = 0
        self.reconnect = reconnect
        self.established = False
        self.disconnected = None
        self.runtime = {}
        self.rate = PlaybackRate(getattr(args, "rate", 1.0), rate_fallback)

    def mark_disconnected(self, cause):
        if not self.closing and self.disconnected is None:
            self.disconnected = cause
            print(f"connection lost: {cause}", file=sys.stderr, flush=True)

    def result(self, reason):
        return {"reason": reason, "last_seconds": self.last_seconds,
                "duration": self.duration, "playback_seconds": self.playback_seconds,
                "runtime": self.runtime, "rate": dict(self.rate.status), "disconnect_cause": self.disconnected,
                "errors": dict(self.errors), "error_samples": list(self.error_samples)}

    def report_error(self, operation, exc, fatal=False):
        self.errors[operation] = self.errors.get(operation, 0) + 1
        message = f"{operation}: {exc}"
        if len(self.error_samples) < 10:
            self.error_samples.append(message[:500])
        if self.errors[operation] <= 3:
            print(message[:300], file=sys.stderr, flush=True)
        if self.established and is_connection_error(exc):
            self.mark_disconnected(str(exc)[:500])
        if fatal or (isinstance(exc, OSError) and not isinstance(exc, TimeoutError)):
            self.fatal = message

    def spawn(self, coroutine):
        if self.closing:
            coroutine.close()
            return
        def finished(task):
            self.tasks.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.report_error("background task", exc)
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(finished)

    async def consider(self, cdp, oid, desc):
        if "Uint8Array" in desc or "ArrayBuffer" in desc:
            result = await send(cdp, "Runtime.callFunctionOn", {
                "objectId": oid, "functionDeclaration": HEAD_JS, "returnByValue": True})
            head = result.get("result", {}).get("value")
            if not head or not MIN_LEN <= head["len"] <= MAX_LEN:
                return
            kind = classify(base64.b64decode(head["head"]))
            if kind is None:
                return
            result = await send(cdp, "Runtime.callFunctionOn", {
                "objectId": oid, "functionDeclaration": FULL_JS, "returnByValue": True})
            self.store.add(kind, base64.b64decode(result["result"]["value"]))
            self.last_media = asyncio.get_running_loop().time()
            self.media_events += 1  # Duplicates during replay still prove capture is healthy.
        elif "EncodedAudioChunk" in desc:
            self.sinks["EncodedAudioChunk"] = self.sinks.get("EncodedAudioChunk", 0) + 1
            result = await send(cdp, "Runtime.callFunctionOn", {
                "objectId": oid, "functionDeclaration": ENC_JS, "returnByValue": True})
            self.store.add_enc(base64.b64decode(result["result"]["value"]))
            self.last_media = asyncio.get_running_loop().time()
            self.media_events += 1
        else:
            name = desc.split("(", 1)[0]
            self.sinks[name] = self.sinks.get(name, 0) + 1

    async def paused(self, cdp, event):
        try:
            seen = set()
            for frame in event.get("callFrames", [])[:5]:
                for scope in frame.get("scopeChain", []):
                    if scope.get("type") not in ("local", "closure", "block", "script"):
                        continue
                    oid = scope.get("object", {}).get("objectId")
                    if not oid:
                        continue
                    try:
                        props = await send(cdp, "Runtime.getProperties", {"objectId": oid, "ownProperties": True})
                    except Exception as exc:
                        self.report_error("scope read", exc)
                        continue
                    for prop in props.get("result", []):
                        value = prop.get("value") or {}
                        desc = value.get("description", "")
                        oid = value.get("objectId")
                        if not oid or oid in seen or not any(t in desc for t in (
                                "Uint8Array", "ArrayBuffer", "EncodedAudioChunk", "AudioData", "AudioBuffer")):
                            continue
                        seen.add(oid)
                        try:
                            await self.consider(cdp, oid, desc)
                        except Exception as exc:
                            self.report_error("capture buffer", exc, fatal=isinstance(exc, CaptureError))
        finally:
            try:
                await send(cdp, "Debugger.resume")
            except Exception as exc:
                if not self.closing:
                    self.report_error("debugger resume", exc)

    async def run(self):
        browser = None
        loop = asyncio.get_running_loop()
        self.last_media = loop.time()
        try:
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(channel="chrome", headless=True)
                    self.established = True
                    browser.on("disconnected", lambda *_: self.mark_disconnected("browser disconnected"))
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.on("crash", lambda *_: self.mark_disconnected("page crashed"))
                    page.on("close", lambda *_: self.mark_disconnected("page closed"))
                    cdp = await context.new_cdp_session(page)
                    cdp.on("close", lambda *_: self.mark_disconnected("CDP session closed"))
                    cdp.on("Inspector.detached", lambda event: self.mark_disconnected(
                        f"CDP detached: {event.get('reason', 'unknown reason')}"))
                    self.tracker = SinkTracker(cdp, self.report_error,
                                               urlsplit(self.args.url).hostname or "")
                    def created(event):
                        cid = self.tracker.created(event)
                        if cid is not None:
                            self.spawn(self.tracker.arm(cid))
                    cdp.on("Runtime.executionContextCreated", created)
                    cdp.on("Runtime.executionContextDestroyed", self.tracker.destroyed)
                    cdp.on("Runtime.executionContextsCleared", self.tracker.cleared)
                    cdp.on("Debugger.paused", lambda event: self.spawn(self.paused(cdp, event)))
                    await send(cdp, "Runtime.enable")
                    await send(cdp, "Debugger.enable")
                    self.spawn(self.tracker.watchdog())
                    url = self.args.url.split("#", 1)[0]
                    if self.args.start and not self.reconnect:
                        url += f"#t={self.args.start}"
                    print(f"goto {url}", flush=True)
                    await page.goto(url, wait_until="load", timeout=60000)
                    if not await self.tracker.arm_all():
                        raise CaptureError("no media capture sink could be armed")
                    await page.wait_for_selector("#player-playpause", timeout=20000)
                    for _ in range(2):
                        result = await send(cdp, "Runtime.evaluate", {"returnByValue": True, "expression":
                            "((document.getElementById('player-playpause')||{}).className||'').includes('paused')"})
                        if not result["result"]["value"]:
                            break
                        await page.click("#player-playpause")
                        await asyncio.sleep(1)
                    return await self.monitor(cdp)
                finally:
                    self.closing = True
                    tasks = list(self.tasks)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if self.tracker is not None:
                        self.armed_sinks = self.tracker.status()
                    if browser is not None:
                        try:
                            await asyncio.wait_for(browser.close(), 10)
                        except Exception as exc:
                            self.report_error("browser close", exc)
        except (KeyboardInterrupt, asyncio.CancelledError):
            return "interrupted"
        except Exception as exc:
            self.report_error("session", exc)
            if not self.fatal and (self.disconnected or (self.established and is_connection_error(exc))):
                self.disconnected = self.disconnected or str(exc)[:500]
                return "connection_lost"
            return "capture_error"

    async def monitor(self, cdp):
        loop = asyncio.get_running_loop()
        began = loop.time()
        try:
            return await self._monitor(cdp, began)
        finally:
            self.playback_seconds = loop.time() - began

    async def _monitor(self, cdp, began):
        loop = asyncio.get_running_loop()
        explicit = self.args.max_seconds is not None
        allowance = max(0, self.args.max_seconds - self.elapsed_playback) if explicit else 1200
        deadline = began + allowance
        self.runtime = {"mode": "explicit" if explicit else "automatic", "allowance_seconds": allowance}
        known_duration = None
        self.last_media = began
        last_progress = began
        previous = -1
        saw_progress = False
        retries = 0
        events_at_retry = self.media_events
        coverage_events = -1
        coverage_end = -1
        while loop.time() < deadline:
            await asyncio.sleep(min(3, deadline - loop.time()))
            if self.fatal:
                return "capture_error"
            if self.disconnected:
                return "connection_lost"
            if loop.time() >= deadline:
                break
            result = await send(cdp, "Runtime.evaluate", {"returnByValue": True, "expression":
                "(document.getElementById('player-progress-text')||{}).innerText||''"})
            text = result.get("result", {}).get("value", "")
            try:
                current, total = [parse_time(part.strip()) for part in text.split("/")]
            except (ValueError, AttributeError):
                if loop.time() - last_progress > self.args.stall_seconds:
                    return "player_unavailable"
                continue
            now = loop.time()
            if not explicit and total > 0 and total != known_duration:
                known_duration = total
                deadline = now + max(0, total - current) + self.args.grace_seconds
                self.runtime["allowance_seconds"] = deadline - began
                print(f"runtime allowance: {total-current}s remaining + {self.args.grace_seconds}s grace", flush=True)
            if previous >= 0 and current > previous:
                last_progress = now
                saw_progress = True
            previous = current
            self.last_seconds, self.duration = current, total
            write_json(self.store.dir / "state.json", {"last_seconds": current, "duration": total})
            if self.media_events > events_at_retry:
                retries = 0
                events_at_retry = self.media_events
            idle = now - self.last_media
            advancing = saw_progress and now - last_progress < 6
            print(f"t={now-began:.0f}s prog={text!r} frags={len(self.store.frags)} idle={idle:.0f}s", flush=True)
            if total > 0 and current >= total:
                return "end"
            if self.rate.status["requested"] > 1 and not self.rate.status["fallback_reason"]:
                try:
                    await self.rate.poll(cdp, self.tracker.contexts, total)
                except Exception as exc:
                    self.rate.status["fallback_reason"] = "rate control unavailable"
                    self.report_error("playback rate", exc)
                    if self.disconnected:
                        return "connection_lost"
                    if self.rate.status["actual"] != 1:
                        return "capture_error"
            if not advancing and idle > self.args.stall_seconds:
                return "stalled"
            if advancing and idle > self.args.broken_seconds:
                # Ahead-of-playback buffers explain quiet capture until consumed.
                if coverage_events != self.media_events:
                    coverage_events = self.media_events
                    raw = self.store.dir / "coverage.m4a"
                    info = self.store.assemble(raw)
                    coverage_end = -1
                    try:
                        if info["init"] and info["frags"] and not info["gaps"]:
                            report = await asyncio.to_thread(probe_media, raw, None, False)
                            coverage_end = report["end"]
                    except Exception as exc:
                        self.report_error("buffer coverage", exc)
                    finally:
                        raw.unlink(missing_ok=True)
                if coverage_end >= current + 1:
                    continue
                if retries >= 3:
                    return "capture_broken"
                retries += 1
                print(f"capture quiet -> renew breakpoints ({retries}/3)", flush=True)
                await self.tracker.arm_all(force=True)
                self.last_media = now
        return "timeout"


def parse_time(value):
    parts = value.split(":")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise ValueError("invalid player time")
    if any(int(part) >= 60 for part in parts[1:]):
        raise ValueError("invalid player time component")
    return sum(int(part) * 60 ** power for power, part in enumerate(reversed(parts)))


def finalize(args, store, session, reason, attempts=None):
    out = Path(args.out).resolve()
    raw = out.with_name(out.stem + ".raw.m4a")
    attempts = attempts or [session.result(reason)]
    errors = {}
    for attempt in attempts:
        for operation, count in attempt["errors"].items():
            errors[operation] = errors.get(operation, 0) + count
    status = {"complete": False, "verified": False, "reason": reason,
              "last_seconds": session.last_seconds, "duration": session.duration,
              "enc_chunks": len(store.enc), "sinks": session.sinks,
              "armed_sinks": session.armed_sinks,
              "errors": errors, "error_samples": [sample for attempt in attempts for sample in attempt["error_samples"]][:10],
              "attempts": attempts, "reconnects": len(attempts) - 1,
              "playback_seconds": sum(attempt["playback_seconds"] for attempt in attempts)}
    try:
        info = store.assemble(raw)
        status.update(info)
        if reason == "end":
            if not info["init"] or not info["frags"]:
                status["reason"] = "missing_media"
            elif info["gaps"]:
                status["reason"] = "end_with_gaps"
            elif session.fatal:
                status["reason"] = "capture_error"
            else:
                status["media"] = probe_media(raw, session.duration, not args.no_verify)
                with staged_path(out) as candidate:
                    if args.no_fix:
                        shutil.copyfile(raw, candidate)
                    else:
                        remux(raw, candidate)
                    status["media"] = probe_media(candidate, session.duration, not args.no_verify)
                    publish(candidate, out)
                status["complete"] = True
                status["verified"] = not args.no_verify
    except Exception as exc:
        status["reason"] = "verification_failed" if reason == "end" else "finalization_failed"
        status["error"] = str(exc)[:500]
        print(f"finalization: {exc}", file=sys.stderr, flush=True)
    try:
        write_json(store.dir / "status.json", status)
    except OSError as exc:
        status.update(complete=False, reason="status_write_failed", error=str(exc))
        print(f"status could not be saved: {exc}", file=sys.stderr, flush=True)
    print("VERDICT:", "COMPLETE" if status["complete"] else f"INCOMPLETE ({status['reason']})",
          json.dumps(status), flush=True)
    return 0 if status["complete"] else 2


async def capture(args):
    out = Path(args.out).resolve()
    if not shutil.which("ffprobe"):
        raise CaptureError("ffprobe is required to validate timeline coverage")
    if not (args.no_verify and args.no_fix) and not shutil.which("ffmpeg"):
        raise CaptureError("ffmpeg is required for decode verification/remux")
    with parts_lock(out.with_suffix(out.suffix + ".parts")):
        store = Store(out.with_suffix(out.suffix + ".parts"), args.url)
        if args.resume:
            print("resume: replaying from the beginning to refill gaps; saved fragments are deduplicated", flush=True)
        write_json(store.dir / "status.json", {"complete": False, "verified": False, "reason": "running"})
        attempts = []
        elapsed_playback = 0
        rate_fallback = None
        for attempt in range(1 if args.no_reconnect else 2):
            session = Session(args, store, elapsed_playback, reconnect=attempt > 0,
                              rate_fallback=rate_fallback)
            reason = await session.run()
            attempts.append(session.result(reason))
            rate_fallback = session.rate.status["fallback_reason"]
            elapsed_playback += session.playback_seconds
            if reason != "connection_lost" or session.fatal or args.no_reconnect or attempt == 1:
                break
            if args.max_seconds is not None and elapsed_playback >= args.max_seconds:
                reason = "timeout"
                break
            try:
                write_json(store.dir / "status.json", {"complete": False, "verified": False,
                           "reason": "reconnecting", "attempts": attempts})
            except OSError as exc:
                session.report_error("reconnect state", exc, fatal=True)
                reason = "capture_error"
                attempts[-1] = session.result(reason)
                break
            print("reconnect 1/1: restarting from zero and reusing persisted fragments", flush=True)
        return finalize(args, store, session, reason, attempts)


def main():
    parser = argparse.ArgumentParser(prog="coldvideo-downloader", description="Browser-assisted media capture using native CDP breakpoints")
    parser.add_argument("url", help="track/post URL")
    parser.add_argument("-o", "--out", default="coldvideo.m4a")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="maximum playback rate from 1 to 16; adapts to available buffer")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("--start", type=int, default=0, help="start offset; a tail alone cannot pass full-track validation")
    start.add_argument("--resume", action="store_true", help="replay from zero and reuse saved fragments")
    parser.add_argument("--max-seconds", type=int, help="hard playback-time limit across attempts; default: duration + grace")
    parser.add_argument("--grace-seconds", type=int, default=60, help="extra runtime after remaining playback (default: 60)")
    parser.add_argument("--no-reconnect", action="store_true", help="disable the single retry after a browser/CDP disconnect")
    parser.add_argument("--stall-seconds", type=int, default=45)
    parser.add_argument("--broken-seconds", type=int, default=20)
    parser.add_argument("--no-verify", action="store_true", help="skip decode checks; timeline validation remains mandatory")
    parser.add_argument("--no-fix", action="store_true", help="publish validated fMP4 without remux")
    args = parser.parse_args()
    if not math.isfinite(args.rate) or not 1 <= args.rate <= 16:
        parser.error("rate must be a finite number from 1 to 16")
    if (args.start < 0 or min(args.grace_seconds, args.stall_seconds, args.broken_seconds) <= 0
            or args.max_seconds is not None and args.max_seconds <= 0):
        parser.error("start must be nonnegative and timeouts must be positive")
    try:
        sys.exit(asyncio.run(capture(args)))
    except (CaptureError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(2)


if __name__ == "__main__":
    main()
