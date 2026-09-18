"""MCP bridge v2.1:工作区沙箱 + argv 执行 + 结构化输出。

安全模型:不再检查 shell 语法(; && |),而是:
  - 路径边界:读/写各自限定在 --readable / --writable 根目录内,--deny 优先拒绝
  - 执行边界:argv[0] 必须在 --executables 白名单内(不走 shell,无注入面)
  - 无 sudo;进程以本机用户权限运行

v2.1:每个工具带 TypedDict 返回类型(生成 outputSchema)与 MCP annotations
(让 ChatGPT 的风险徽章如实反映只读/可写/是否触网)。
"""

import argparse
import fnmatch
import os
import secrets
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=8000)
parser.add_argument("--token", default=secrets.token_urlsafe(24))
parser.add_argument("--writable", default="/media/zhao/add,/home/zhao/chatgpt-downloads,/tmp/chatgpt-mcp-jobs")
parser.add_argument("--readable", default="/media/zhao/add,/home/zhao,/tmp/chatgpt-mcp-jobs")
parser.add_argument("--deny", default="/home/zhao/.ssh,/home/zhao/.gnupg,/home/zhao/.aws,/home/zhao/.config/gh,/etc,/boot,/root,/run")
parser.add_argument("--executables", default=(
    "python,python3,pip,pip3,conda,git,nvidia-smi,make,cmake,gcc,g++,node,npm,"
    "ls,cat,head,tail,grep,find,wc,df,du,free,uname,whoami,date,ps,top,uptime,"
    "which,hostname,ip,file,stat,tar,zip,unzip,curl,wget,bash,sh,sort,uniq,echo,mkdir,touch,rm,mv,cp,ln,chmod"))
args = parser.parse_args()


def _roots(spec: str) -> list[Path]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser()
        try:
            out.append(p.resolve() if p.exists() else p)
        except OSError:
            out.append(p)
    return out


WRITABLE = _roots(args.writable)
READABLE = _roots(args.readable)
DENY = _roots(args.deny)
EXEC = {e.strip() for e in args.executables.split(",") if e.strip()}

JOBS_DIR = Path("/tmp/chatgpt-mcp-jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)
JOBS: dict[str, dict] = {}

mcp = MCPServer("home-computer")


# ---------------------------------------------------------------- 返回类型

class Res(TypedDict, total=False):
    error: str


class RunResult(Res, total=False):
    exit_code: int
    stdout: str
    stderr: str
    elapsed_secs: float
    cwd: str


class JobStart(Res, total=False):
    job_id: str
    pid: int
    started_at: str
    cwd: str
    stdout_file: str
    stderr_file: str


class JobStatus(Res, total=False):
    job_id: str
    status: str
    pid: int
    elapsed_secs: float
    exit_code: int | None
    argv: list[str]
    cwd: str
    cpu_percent: float
    rss_mb: float
    gpu_memory: str
    stdout_tail: str
    stderr_tail: str


class KillResult(Res, total=False):
    job_id: str
    killed: bool
    signal: str
    already_finished: bool
    exit_code: int | None


class ReadResult(Res, total=False):
    path: str
    total_lines: int
    start_line: int
    end_line: int
    truncated: bool
    content: str


class WriteResult(Res, total=False):
    path: str
    bytes: int
    mode: str


class PatchResult(Res, total=False):
    changed: bool
    path: str
    hunks_applied: int


class DirEntry(TypedDict, total=False):
    name: str
    type: str
    size: int
    mtime: str
    path: str


class ListResult(Res, total=False):
    path: str
    count: int
    entries: list[DirEntry]


class SearchResults(Res, total=False):
    root: str
    query: str
    count: int
    truncated: bool
    matches: list[str]


class FindResults(Res, total=False):
    root: str
    glob: str
    count: int
    truncated: bool
    files: list[str]


class DownloadResult(Res, total=False):
    url: str
    path: str
    bytes: int


# 注解:local=不触网的本机操作;read=只读;write=可改文件;exec=可执行代码
ANN_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
ANN_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
ANN_EXEC = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
ANN_NET = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


# ---------------------------------------------------------------- 边界检查

def _err(msg: str) -> dict:
    return {"error": msg}


def _resolve_in(path: str, roots: list[Path], what: str) -> Path | dict:
    p = Path(path).expanduser()
    try:
        p = p.resolve() if p.exists() else Path(os.path.abspath(p))
    except OSError:
        return _err(f"bad path: {path}")
    for d in DENY:
        if p == d or p.is_relative_to(d):
            return _err(f"{what} denied by rule: {path} is under {d}")
    for r in roots:
        if p == r or p.is_relative_to(r):
            return p
    allowed = ", ".join(str(r) for r in roots)
    return _err(f"{what} outside allowed roots: {path} (allowed: {allowed})")


def _read_path(path: str) -> Path | dict:
    return _resolve_in(path, READABLE, "read")


def _write_path(path: str) -> Path | dict:
    return _resolve_in(path, WRITABLE, "write")


def _check_argv(argv: list[str]) -> str | None:
    if not argv or not argv[0]:
        return "empty argv"
    exe = Path(argv[0]).name
    if exe not in EXEC:
        return f"executable '{exe}' not allowed (add via --executables)"
    return None


# 文件操作类命令:参数里的路径也要过沙箱(否则 rm/mv 可越界删改任意文件)
MUTATORS = {"rm", "mv", "cp", "ln", "chmod", "chown", "touch", "mkdir", "rmdir",
            "truncate", "tee", "dd", "tar", "zip", "unzip"}
READERS = {"cat", "head", "tail", "grep", "find", "ls", "file", "stat", "wc", "du", "sort", "uniq"}


def _check_argv_paths(exe: str, argv: list[str], wd: Path) -> str | None:
    roots = WRITABLE if exe in MUTATORS else READABLE
    if exe not in MUTATORS and exe not in READERS:
        return None
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        if "/" not in tok and not tok.startswith("~"):
            continue  # 裸文件名按 cwd 解析,天然在沙箱内
        p = Path(tok).expanduser()
        p = p if p.is_absolute() else wd / p
        try:
            p = p.resolve() if p.exists() else Path(os.path.abspath(p))
        except OSError:
            continue
        for d in DENY:
            if p == d or p.is_relative_to(d):
                return f"argv path denied by rule: {p} is under {d}"
        if not any(p == r or p.is_relative_to(r) for r in roots):
            return f"argv path outside sandbox: {p} (allowed: {', '.join(map(str, roots))})"
    return None


def _cwd(cwd: str | None) -> Path | dict:
    if cwd is None:
        return WRITABLE[0]
    return _read_path(cwd)


def _merged_env(env: dict | None) -> dict | None:
    if not env:
        return None
    merged = dict(os.environ)
    merged.update({str(k): str(v) for k, v in env.items()})
    return merged


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[-n:]


# ---------------------------------------------------------------- 执行

@mcp.tool(annotations=ANN_EXEC)
def run_command(argv: list[str], cwd: str | None = None, env: dict | None = None,
                timeout_secs: int = 60, max_output_chars: int = 20000) -> RunResult:
    """执行一条命令(argv 数组,不走 shell,支持任意参数)。返回 exit_code/stdout/stderr/elapsed。

    例:argv=["python3","-u","run.py"]。可执行文件需在白名单;cwd 必须在可读目录内。
    超过 300 秒的任务请改用 start_process。
    """
    if e := _check_argv(argv):
        return _err(e)
    wd = _cwd(cwd)
    if isinstance(wd, dict):
        return wd
    if e := _check_argv_paths(Path(argv[0]).name, argv, wd):
        return _err(e)
    timeout_secs = max(1, min(timeout_secs, 300))
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=str(wd), env=_merged_env(env),
                           capture_output=True, text=True, timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        return _err(f"timed out after {timeout_secs}s; use start_process for long jobs")
    except FileNotFoundError:
        return _err(f"executable not found: {argv[0]}")
    except OSError as e:
        return _err(f"spawn failed: {e}")
    return {
        "exit_code": r.returncode,
        "stdout": _clip(r.stdout, max_output_chars),
        "stderr": _clip(r.stderr, max_output_chars),
        "elapsed_secs": round(time.time() - t0, 2),
        "cwd": str(wd),
    }


@mcp.tool(annotations=ANN_EXEC)
def run_python(code: str, cwd: str | None = None, env: dict | None = None,
               timeout_secs: int = 60) -> RunResult:
    """直接执行一段 Python 代码(等价 python3 -c,但无需处理 shell 引号)。"""
    return run_command(["python3", "-c", code], cwd=cwd, env=env, timeout_secs=timeout_secs)


@mcp.tool(annotations=ANN_EXEC)
def start_process(argv: list[str], cwd: str | None = None, env: dict | None = None,
                  stdout_file: str | None = None, stderr_file: str | None = None) -> JobStart:
    """后台启动长任务(训练/实验),立即返回 job_id。输出默认落盘,check_process 可看进度。

    进程在独立进程组中运行,kill_process 会连同子进程一起终止。
    """
    if e := _check_argv(argv):
        return _err(e)
    wd = _cwd(cwd)
    if isinstance(wd, dict):
        return wd
    if e := _check_argv_paths(Path(argv[0]).name, argv, wd):
        return _err(e)
    job_id = uuid.uuid4().hex[:12]
    out = Path(stdout_file) if stdout_file else JOBS_DIR / f"{job_id}.out"
    err = Path(stderr_file) if stderr_file else JOBS_DIR / f"{job_id}.err"
    for f in (out, err):
        chk = _write_path(str(f))
        if isinstance(chk, dict):
            return chk
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        fh_out, fh_err = open(out, "w"), open(err, "w")
        proc = subprocess.Popen(argv, cwd=str(wd), env=_merged_env(env),
                                stdout=fh_out, stderr=fh_err, start_new_session=True)
    except OSError as e:
        return _err(f"spawn failed: {e}")
    JOBS[job_id] = {"proc": proc, "out": out, "err": err, "argv": argv,
                    "cwd": str(wd), "t0": time.time()}
    return {"job_id": job_id, "pid": proc.pid,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "cwd": str(wd), "stdout_file": str(out), "stderr_file": str(err)}


def _tail(path: Path, lines: int) -> str:
    try:
        data = path.read_bytes()[-200000:]
    except OSError:
        return ""
    return "\n".join(data.decode(errors="replace").splitlines()[-lines:])


@mcp.tool(annotations=ANN_READ)
def check_process(job_id: str, tail_lines: int = 100) -> JobStatus:
    """查看后台任务:状态/运行时长/CPU/内存/GPU 占用 + stdout/stderr 尾部。"""
    job = JOBS.get(job_id)
    if job is None:
        return _err(f"unknown job_id {job_id}; known: {', '.join(JOBS) or 'none'}")
    proc = job["proc"]
    rc = proc.poll()
    if rc is None:
        status = "running"
    elif rc in (-9, -15) or rc in (137, 143):
        status = "killed"
    elif rc == 0:
        status = "finished"
    else:
        status = "failed"
    info: JobStatus = {"job_id": job_id, "status": status, "pid": proc.pid,
                       "elapsed_secs": round(time.time() - job["t0"], 1),
                       "exit_code": rc, "argv": job["argv"], "cwd": job["cwd"]}
    if rc is None:  # 运行中才采资源
        try:
            r = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(proc.pid)],
                               capture_output=True, text=True, timeout=10)
            parts = r.stdout.split()
            if len(parts) >= 2:
                info["cpu_percent"] = float(parts[0])
                info["rss_mb"] = round(float(parts[1]) / 1024, 1)
        except Exception:
            pass
        try:
            r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                if line.strip().startswith(f"{proc.pid},"):
                    info["gpu_memory"] = line.split(",", 1)[1].strip()
                    break
        except Exception:
            pass
    info["stdout_tail"] = _tail(job["out"], tail_lines)
    info["stderr_tail"] = _tail(job["err"], tail_lines)
    return info


@mcp.tool(annotations=ANN_EXEC)
def kill_process(job_id: str, signal_name: str = "TERM") -> KillResult:
    """终止后台任务。默认 TERM 且杀整个进程组(子进程一并清理);顽固进程用 KILL。"""
    job = JOBS.get(job_id)
    if job is None:
        return _err(f"unknown job_id {job_id}")
    proc = job["proc"]
    if proc.poll() is not None:
        return {"job_id": job_id, "already_finished": True, "exit_code": proc.returncode}
    sig = signal.SIGKILL if signal_name.upper() == "KILL" else signal.SIGTERM
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    return {"job_id": job_id, "killed": True, "signal": signal_name.upper()}


# ---------------------------------------------------------------- 文件

@mcp.tool(annotations=ANN_READ)
def read_file(path: str, start_line: int | None = None, end_line: int | None = None,
              max_chars: int = 50000) -> ReadResult:
    """读文本文件,可指定行区间(1-based,含端点)。返回内容与总行数。"""
    p = _read_path(path)
    if isinstance(p, dict):
        return p
    if not p.is_file():
        return _err(f"not a file: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    a = max(1, start_line or 1)
    b = min(total, end_line or total)
    seg = "\n".join(lines[a - 1:b])
    truncated = len(seg) > max_chars
    return {"path": str(p), "total_lines": total, "start_line": a, "end_line": b,
            "truncated": truncated, "content": seg[:max_chars]}


@mcp.tool(annotations=ANN_WRITE)
def write_file(path: str, content: str, mode: str = "create",
               atomic: bool = True, create_parents: bool = True) -> WriteResult:
    """写文本文件。mode: create(已存在则报错)/ overwrite / append。

    atomic=True 时先写临时文件再 rename,避免写一半的损坏文件。
    """
    p = _write_path(path)
    if isinstance(p, dict):
        return p
    if mode not in ("create", "overwrite", "append"):
        return _err(f"bad mode {mode}")
    if mode == "create" and p.exists():
        return _err(f"exists (use overwrite/append): {p}")
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    if atomic:
        tmp = p.parent / f".{p.name}.tmp-{uuid.uuid4().hex[:8]}"
        with open(tmp, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    else:
        with open(p, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
    return {"path": str(p), "bytes": len(content.encode()), "mode": mode}


def _parse_patch(patch: str) -> list[dict]:
    hunks, cur = [], None
    for line in patch.splitlines():
        if line.startswith("@@"):
            if cur:
                hunks.append(cur)
            cur = {"body": []}
            continue
        if cur is not None and line[:1] in (" ", "-", "+"):
            cur["body"].append(line)
    if cur:
        hunks.append(cur)
    return hunks


@mcp.tool(annotations=ANN_WRITE)
def apply_patch(path: str, patch: str) -> PatchResult:
    """对文件应用 unified diff(可省略 @@ 行号,按上下文定位)。

    上下文不匹配时拒绝修改并报错,不会写坏文件。适合小范围精准编辑。
    """
    p = _write_path(path)
    if isinstance(p, dict):
        return p
    if not p.is_file():
        return _err(f"not a file: {p}")
    hunks = _parse_patch(patch)
    if not hunks:
        return _err("no hunks found in patch")
    lines = p.read_text(encoding="utf-8").splitlines()
    applied = 0
    for h in hunks:
        old = [l[1:] for l in h["body"] if l[0] in (" ", "-")]
        new = [l[1:] for l in h["body"] if l[0] in (" ", "+")]
        if not old:
            return _err(f"hunk {applied} has no context; refusing")
        pos = None
        for i in range(len(lines) - len(old) + 1):
            if lines[i:i + len(old)] == old:
                pos = i
                break
        if pos is None:
            return _err(f"hunk {applied} context not found in {p.name}; nothing written")
        lines[pos:pos + len(old)] = new
        applied += 1
    tmp = p.parent / f".{p.name}.tmp-{uuid.uuid4().hex[:8]}"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return {"changed": True, "path": str(p), "hunks_applied": applied}


@mcp.tool(annotations=ANN_READ)
def list_dir(path: str, recursive: bool = False, max_depth: int = 1,
             glob: str | None = None) -> ListResult:
    """列目录,返回结构化条目(name/type/size/mtime/path)。"""
    p = _read_path(path)
    if isinstance(p, dict):
        return p
    if not p.is_dir():
        return _err(f"not a directory: {p}")
    out: list[DirEntry] = []

    def walk(d: Path, depth: int):
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name))
        except OSError:
            return
        for c in entries:
            if any(c == dn or c.is_relative_to(dn) for dn in DENY):
                continue
            if glob and not fnmatch.fnmatch(c.name, glob):
                ok = False
            else:
                ok = True
            if ok:
                st = c.stat()
                out.append({"name": c.name, "type": "dir" if c.is_dir() else "file",
                            "size": st.st_size,
                            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                            "path": str(c)})
            if c.is_dir() and recursive and depth < max_depth:
                walk(c, depth + 1)
            if len(out) >= 2000:
                return

    walk(p, 1)
    return {"path": str(p), "count": len(out), "entries": out}


@mcp.tool(annotations=ANN_READ)
def search_text(root: str, query: str, glob: str = "**/*.py", max_results: int = 100,
                case_sensitive: bool = False) -> SearchResults:
    """在工作区内按 glob 搜索文本,返回 文件:行号: 内容 列表。"""
    r = _read_path(root)
    if isinstance(r, dict):
        return r
    if not r.is_dir():
        return _err(f"not a directory: {r}")
    needle = query if case_sensitive else query.lower()
    matches = []
    for f in r.rglob(glob if "*" in glob else f"**/{glob}"):
        try:
            if not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            if any(f == dn or f.is_relative_to(dn) for dn in DENY):
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    matches.append(f"{f.relative_to(r)}:{i}: {line.strip()[:200]}")
                    if len(matches) >= max_results:
                        return {"root": str(r), "query": query, "count": len(matches),
                                "truncated": True, "matches": matches}
        except OSError:
            continue
    return {"root": str(r), "query": query, "count": len(matches),
            "truncated": False, "matches": matches}


@mcp.tool(annotations=ANN_READ)
def find_files(root: str, glob: str = "**/*", max_results: int = 1000) -> FindResults:
    """在工作区内按 glob 查找文件路径。"""
    r = _read_path(root)
    if isinstance(r, dict):
        return r
    if not r.is_dir():
        return _err(f"not a directory: {r}")
    found = []
    for f in r.rglob(glob):
        if any(f == dn or f.is_relative_to(dn) for dn in DENY):
            continue
        found.append(str(f.relative_to(r)))
        if len(found) >= max_results:
            return {"root": str(r), "glob": glob, "count": len(found), "truncated": True, "files": found}
    return {"root": str(r), "glob": glob, "count": len(found), "truncated": False, "files": found}


@mcp.tool(annotations=ANN_NET)
def download_file(url: str, filename: str = "", dest_dir: str | None = None) -> DownloadResult:
    """从 http(s) 链接下载文件到工作区(上限 500MB)。dest_dir 默认第一个可写根目录。"""
    if not url.startswith(("http://", "https://")):
        return _err("only http(s) URLs are allowed")
    base = _write_path(dest_dir or str(WRITABLE[0]))
    if isinstance(base, dict):
        return base
    name = filename or url.split("?")[0].rstrip("/").split("/")[-1] or "download.bin"
    p = _write_path(str(base / name))
    if isinstance(p, dict):
        return p
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mcp-bridge/2.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(p, "wb") as f:
            total = 0
            while chunk := r.read(1 << 20):
                total += len(chunk)
                if total > 500 * 1024 * 1024:
                    f.close()
                    p.unlink()
                    return _err("file exceeds the 500MB limit")
                f.write(chunk)
    except Exception as e:
        return _err(f"{e}")
    return {"url": url, "path": str(p), "bytes": total}


if __name__ == "__main__":
    print(f"MCP endpoint: http://127.0.0.1:{args.port}/mcp/{args.token}")
    print(f"writable: {', '.join(map(str, WRITABLE))}")
    print(f"executables: {len(EXEC)} allowed")
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=args.port,
        streamable_http_path=f"/mcp/{args.token}",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
