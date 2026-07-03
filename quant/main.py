"""CLI. `python quant.py` serves; --doctor checks everything; --once prints a
snapshot; --backtest replays your own tick history walk-forward;
--backtest-sweep grid-searches the DIP entry knobs over that same history."""
import json
import secrets
import sys
import threading
import time
import webbrowser

from . import __version__, config, store
from .engine import poll
from .server import serve
from .sources import LiveIO, NINJA_TYPES, resolve_league


def poller_loop(io):
    last_update_check = 0.0
    while True:
        cfg = config.load()
        try:
            s = poll(cfg, io)
            st = s.get("stats", {})
            print(f"[{s['ts']}] {s.get('league')} ex/div={s.get('ex_per_div')} "
                  f"scanned={st.get('scanned')} proposals={st.get('proposals')} "
                  f"cards={len(s.get('cards', []))} shadow={st.get('shadow_open')}"
                  + (f" warn={s['errors']}" if s.get("errors") else ""))
        except Exception as e:
            print("poll failed:", e)
        if time.time() - last_update_check > 3 * 3600:  # check GitHub a few times a day
            last_update_check = time.time()
            try:
                refresh_update_status(cfg)
            except Exception:
                pass
        time.sleep(max(2, int(cfg["adv"].get("poll_minutes", 5))) * 60)


def refresh_update_status(cfg):
    from . import update
    res = update.check(cfg["update_branch"], token=update.token_from(cfg))
    c = store.connect(config.DB_PATH)
    store.kv_set_json(c, "update_status", {**res, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    c.commit()
    c.close()
    if res.get("available"):
        print(f"UPDATE available: {res['current']} → {res['latest']} "
              "(open the dashboard to install, or it auto-applies if enabled)")
    return res


def auto_bootstrap(io):
    """First-run calibration from league history, in the background so serving
    starts instantly. Idempotent — skips once done for the league."""
    cfg = config.load()
    if not cfg.get("auto_bootstrap"):
        return
    try:
        from .bootstrap import run as boot_run
        boot_run(cfg, io=io, log=lambda m: print("  " + m))
    except Exception as e:
        print("auto-bootstrap skipped:", e)


def doctor():
    cfg, io, ok = config.load(), LiveIO(), True
    print(f"QUANT {__version__} doctor")
    try:
        rows = io.leagues()
        cur = ", ".join(l["Value"] for l in rows if l.get("IsCurrent")) or "?"
        print(f"PASS poe2scout /Leagues     {len(rows)} leagues, current: {cur}")
    except Exception as e:
        ok = False
        print("FAIL poe2scout /Leagues:", e)
    info = resolve_league(io, cfg)
    league = info["name"]
    print(f"INFO league '{cfg['league']}' -> '{league}'"
          + (f"  [{info['note']}]" if info.get("note") else ""))
    rate = None
    for t in cfg["adv"].get("scan_types") or NINJA_TYPES:
        try:
            d = io.ninja(league, t)
            rate = rate or d.get("ex_per_div")
            print(f"PASS poe.ninja {t:<18} {len(d['price_ex'])} items")
        except Exception as e:
            ok = False
            print(f"FAIL poe.ninja {t:<18} {e}")
        io.sleep(0.3)
    try:
        routes, note = io.pairs(league, rate)
        print(f"PASS poe2scout pairs        {len(routes)} items with major routes"
              + (f"  [{note}]" if note else ""))
    except Exception as e:
        print("FAIL poe2scout pairs:", e)
    c = store.connect(config.DB_PATH)
    n_t = c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    n_e = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_p = c.execute("SELECT COUNT(*), SUM(outcome IS NOT NULL) FROM predictions").fetchone()
    cal_ts = store.kv_get(c, "ou_ts")
    c.close()
    print(f"INFO db: {n_t} ticks · {n_e} events · {n_p[0]} predictions ({n_p[1] or 0} graded) · "
          f"models refit {cal_ts or 'never'}")
    print("INFO dry-run poll (nothing stored):")
    snap = poll(cfg, io, store_snap=False)
    print(f"     {snap['stats']['scanned']} scanned, {snap['stats']['proposals']} proposals, "
          f"{len(snap['cards'])} cards")
    for card in snap["cards"]:
        print(f"     CARD [{card['act']}] {card['head']}")
    for e in snap.get("errors", []):
        print("     warn:", e)
    print("Doctor", "OK — run: python quant.py" if ok else "found failures — see above.")
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--doctor" in argv or "--probe" in argv:
        sys.exit(doctor())
    if "--once" in argv:
        print(json.dumps(poll(config.load(), LiveIO(), store_snap=False), indent=2))
        return
    if "--backtest-sweep" in argv:
        from .backtest import DEFAULT_GRID, sweep
        sweep(config.load(), DEFAULT_GRID)
        return
    if "--backtest" in argv:
        from .backtest import run
        run(config.load())
        return
    if "--export" in argv:
        i = argv.index("--export")
        out = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else "quant_export"
        c = store.connect(config.DB_PATH)
        archive = config.DB_PATH.parent / "data_archive"
        res = store.export_all(c, out, archive_dir=archive)
        c.close()
        print(f"Exported to {res['dir']}/ — {res['predictions']} forecasts, "
              f"{res['bars']} hourly bars, {res['ticks_live']} live ticks. "
              f"Full-resolution tick archive: {res['archive_files']} monthly file(s) in "
              f"{archive}/ (kept forever).")
        return
    if "--bootstrap" in argv:
        from .bootstrap import run as boot_run
        i = argv.index("--bootstrap")
        top = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 150
        boot_run(config.load(), top_n=top, force=True)
        return
    if "--update" in argv:
        from . import update
        cfg = config.load()
        tok = update.token_from(cfg)
        res = update.check(cfg["update_branch"], token=tok)
        print(f"current {res['current']} · latest {res.get('latest')} · "
              + ("update available" if res.get("available")
                 else res.get("err") or "up to date"))
        if res.get("available"):
            r = update.apply(cfg["update_branch"], token=tok)
            print("updated to", r.get("version")) if r.get("ok") else print("update failed:", r.get("err"))
        return
    host = "127.0.0.1"
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    port = 8377
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    token = secrets.token_urlsafe(9) if host not in ("127.0.0.1", "localhost") else None
    cfg = config.load()
    io = LiveIO()
    # update check + auto-apply on startup (auto-apply only if the user opted in)
    if "--no-update" not in argv:
        try:
            res = refresh_update_status(cfg)
            if res.get("available") and cfg.get("auto_update"):
                from . import update
                print(f"auto-update: installing {res['latest']}…")
                if update.apply(cfg["update_branch"], token=update.token_from(cfg)).get("ok"):
                    print("auto-update: restarting into the new version…")
                    update.restart()
        except Exception as e:
            print("update check skipped:", e)
    threading.Thread(target=poller_loop, args=(io,), daemon=True).start()
    threading.Thread(target=auto_bootstrap, args=(io,), daemon=True).start()
    try:
        httpd = serve(io, host, port, token)
    except OSError as e:
        print(f"Port {port} is busy — is QUANT already running? ({e})")
        sys.exit(1)
    url = f"http://{'localhost' if host == '127.0.0.1' else host}:{port}" + (f"/?t={token}" if token else "")
    print(f"QUANT {__version__} → {url}  (Ctrl+C to stop)")
    if token:
        print("LAN mode: the token in that URL is required from other devices.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    httpd.serve_forever()
