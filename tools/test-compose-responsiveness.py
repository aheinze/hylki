#!/usr/bin/env python3
"""Exercise Reply and New Message against a healthy, hung or absent EDS.

Run after cargo build, on a disposable desktop/container (Hylki's demo mode
and temporary XDG dirs keep the tests away from real accounts and mail):

    xvfb-run -a python3 tools/test-compose-responsiveness.py target/debug/hylki
    xvfb-run -a python3 tools/test-compose-responsiveness.py target/debug/hylki --mode hang --windowed

Needs Python GObject/GIO and dbus-daemon. Each case gets a private session
bus, so the fake service never replaces the desktop's actual EDS. Screenshots
and logs are kept under --output. No messages are sent.
"""

import argparse
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


DEST = "org.gnome.evolution.dataserver.Sources5"
PATH = "/org/gnome/evolution/dataserver/SourceManager"
IFACE = "org.freedesktop.DBus.ObjectManager"
APP = "co.hyprlab.Hylki"
APP_PATH = "/co/hyprlab/Hylki"
MARKER = "hylki-compose-responsiveness-ok"


def serve(mode, folder):
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    xml = f"""<node><interface name="{IFACE}">
      <method name="GetManagedObjects"><arg type="a{{oa{{sa{{sv}}}}}}" direction="out"/></method>
    </interface></node>"""
    pending = []

    def called(connection, sender, path, interface, method, params, invocation):
        with (folder / "requests.txt").open("a") as out:
            out.write(f"{time.monotonic()} {method}\n")
        if mode == "hang":
            # Hold the call forever, including after the client's timeout.
            pending.append(invocation)
            return
        invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", ({
            PATH + "/test": {"org.gnome.evolution.dataserver.Source": {
                "UID": GLib.Variant("s", "test"),
                "Data": GLib.Variant("s", "[Data Source]\nDisplayName=Test\n[Address Book]\nBackendName=local\n"),
            }},
        },)))

    info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
    conn.register_object(PATH, info, called, None, None)
    conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                   "org.freedesktop.DBus", "RequestName", GLib.Variant("(su)", (DEST, 0)),
                   None, Gio.DBusCallFlags.NONE, 1000, None)
    (folder / "ready").touch()
    GLib.MainLoop().run()


def seed(folder):
    for part in ("config", "data", "cache"):
        (folder / part / "hylki").mkdir(parents=True)
    # One active and one stale EDS cache. A healthy registry excludes the
    # stale one; timeout/unavailability uses both cached books as before.
    for uid in ("test", "stale"):
        book = folder / "data/evolution/addressbook" / uid
        book.mkdir(parents=True)
        with sqlite3.connect(book / "contacts.db") as db:
            db.executescript("CREATE TABLE folder_id (uid TEXT, vcard TEXT);"
                             "CREATE TABLE folder_id_email_list (uid TEXT, value TEXT);")
            card = f"BEGIN:VCARD\nVERSION:3.0\nUID:{uid}\nFN:{uid} Contact\nEMAIL:{uid}@example.org\nEND:VCARD\n"
            db.execute("INSERT INTO folder_id VALUES (?, ?)", (uid, card))
            db.execute("INSERT INTO folder_id_email_list VALUES (?, ?)", (uid, f"{uid}@example.org"))
    with sqlite3.connect(folder / "data/hylki/cache.db") as db:
        db.execute("CREATE TABLE addresses (email TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO addresses VALUES ('history@example.org', 'History Contact', 3)")
    (folder / "edit.js").write_text(
        f"document.body.focus(); document.execCommand('insertText', false, '{MARKER}');\n")


def stop(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def run_case(binary, scenario, mode, folder, windowed):
    folder.mkdir(parents=True)
    seed(folder)
    if windowed:
        (folder / "config/hylki/privacy.toml").write_text("compose_inline = false\nreply_inline = false\n")
    env = os.environ.copy()
    # Do not inherit another showcase's actions (especially send/save).
    for key in list(env):
        if key.startswith("HYLKI_"):
            del env[key]
    env.update({
        "XDG_CONFIG_HOME": str(folder / "config"),
        "XDG_DATA_HOME": str(folder / "data"),
        "XDG_CACHE_HOME": str(folder / "cache"),
        "HYLKI_DEMO": "1", "HYLKI_SHOWCASE": str(folder / "composer.png"),
        "HYLKI_SHOWCASE_DELAY": "10", "HYLKI_SHOWCASE_EDITOR_JS": str(folder / "edit.js"),
        "HYLKI_SHOWCASE_" + scenario.upper(): "4",
        "GSETTINGS_BACKEND": "memory", "GSK_RENDERER": "cairo",
        "RUST_LOG": "hylki=debug", "NO_AT_BRIDGE": "1",
    })
    if windowed:
        env["HYLKI_SHOWCASE_TOP"] = "1"
    bus = service = app = None
    try:
        # No service directories: the absent case must not activate a real
        # registry (or any other desktop service) on this test bus.
        bus_config = folder / "bus.conf"
        bus_config.write_text('''<busconfig><type>session</type>
          <listen>unix:tmpdir=/tmp</listen><auth>EXTERNAL</auth>
          <policy context="default"><allow user="*"/><allow own="*"/>
          <allow send_destination="*"/><allow receive_sender="*"/></policy>
        </busconfig>''')
        bus = subprocess.Popen(["dbus-daemon", "--config-file=" + str(bus_config), "--nofork", "--print-address=1"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        if mode != "absent":
            service = subprocess.Popen([sys.executable, __file__, "--serve", mode, "--output", str(folder)],
                                       env=env, start_new_session=True)
            deadline = time.monotonic() + 5
            while not (folder / "ready").exists():
                if service.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("fake contact service failed to start")
                time.sleep(0.05)
        conn = Gio.DBusConnection.new_for_address_sync(
            env["DBUS_SESSION_BUS_ADDRESS"],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        with (folder / "app.log").open("w") as log:
            app = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=log, start_new_session=True)
            start = time.monotonic()
            probes = []
            while time.monotonic() - start < 15:
                if app.poll() is not None:
                    raise AssertionError(f"Hylki exited with {app.returncode}")
                # List is dispatched by GApplication's main context: unlike
                # Peer.Ping, this checks the GTK loop, not just its bus thread.
                if time.monotonic() - start >= 3:
                    before = time.monotonic()
                    conn.call_sync(APP, APP_PATH, "org.gtk.Actions", "List", None, None,
                                   Gio.DBusCallFlags.NONE, 1000, None)
                    probes.append(time.monotonic() - before)
                text = (folder / "app.log").read_text()
                if (folder / "composer.png").exists() and "showcase editor body:" in text and MARKER in text:
                    break
                time.sleep(0.1)
            assert len(probes) >= 5, "not enough GTK main-loop responses"
            assert "compose fold:" in text, "composer was never mapped"
            assert "showcase editor body:" in text and MARKER in text, "editor did not accept text"
            assert (folder / "composer.png").exists(), "GTK screenshot timer did not run"
            expected = 2 if mode == "healthy" else 3
            assert f"composer suggestions loaded: {expected}" in text, "contacts/history fallback did not arrive"
            if mode != "absent":
                assert (folder / "requests.txt").exists(), "EDS was never queried"
            if mode == "hang":
                assert "EDS source registry unavailable:" in text, "hung request did not time out"
            host = "window" if windowed else "inline"
            print(f"PASS {scenario}/{mode}/{host}: editor accepts text; {len(probes)} GTK replies, "
                  f"slowest {max(probes):.3f}s; {expected} suggestions", flush=True)
    finally:
        stop(app)
        stop(service)
        stop(bus)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", nargs="?", default="target/debug/hylki")
    parser.add_argument("--output", type=Path, default=Path("target/compose-responsiveness"))
    parser.add_argument("--scenario", choices=("new", "reply"))
    parser.add_argument("--mode", choices=("hang", "healthy", "absent"))
    parser.add_argument("--windowed", action="store_true", help="open composers in separate windows")
    parser.add_argument("--serve", choices=("hang", "healthy"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.output)
        return
    binary = Path(args.binary).resolve()
    output = args.output.resolve() / str(time.time_ns())
    print(f"Artifacts: {output}", flush=True)
    failed = 0
    for mode in ([args.mode] if args.mode else ["hang", "healthy", "absent"]):
        for scenario in ([args.scenario] if args.scenario else ["new", "reply"]):
            try:
                run_case(binary, scenario, mode, output / f"{scenario}-{mode}", args.windowed)
            except (AssertionError, GLib.Error) as error:
                failed += 1
                print(f"FAIL {scenario}/{mode}: {error}", flush=True)
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
