
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nostr_cli.py - Interactive Nostr CLI (menu based, no command line args)

Features:
  1) Batch generate private keys                (saved to keys.txt)
  2) Batch import private keys                  (nsec or hex, one per line)
  3) List / switch accounts
  4) Publish notes in batch                     (each message ends with END)
  5) Publish long article (NIP-23, kind 30023)  (title required, content ends with END)
  6) Fetch latest events from relays
  7) Show configuration (relay list / keys file)
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from nostr.event import Event                    # Nostr event object (kind 1 / 30023)
from nostr.filter import Filter                  # subscription filter used when fetching
from nostr.key import PrivateKey                 # key generate / import / sign
from nostr.relay_manager import RelayManager     # relay connect / publish / subscribe

# =========================================================================
# Configuration - edit the relay list below (custom relays are just a list)
# =========================================================================

RELAYS = [                                       # custom relay list, edit here
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://relay.nostr.band",
]

KEY_FILE = Path("keys.txt")                      # local key store, one nsec per line
END_MARKER = "END"                               # terminator of every multi-line input
DEFAULT_KEY_COUNT = 5                            # default amount of keys to generate
DEFAULT_FETCH_LIMIT = 30                         # default amount of events to fetch
KIND_NOTE = 1                                    # short note / short article
KIND_LONGFORM = 30023                            # long article (NIP-23)
PUBLISH_INTERVAL = 1.0                           # seconds between two events (rate limit)
PUBLISH_ACK_TIMEOUT = 12.0                       # seconds to wait for relay OK replies
FETCH_TIMEOUT = 10.0                             # seconds to wait for relay results

# =========================================================================
# Global state
# =========================================================================

accounts = []                                    # all loaded private keys (PrivateKey)
current_index = 0                                # index of the active account


# =========================================================================
# Small helpers
# =========================================================================

def echo(message=""):
    """Print one line (thin wrapper so output can be redirected later)."""
    print(message)


def pause():
    """Keep the menu on screen until the user presses Enter."""
    input("\nPress Enter to continue...")


def read_line(prompt):
    """Read one line and trim surrounding spaces."""
    return input(prompt).strip()


def read_required(prompt):
    """Read one line and repeat until it is not empty."""
    value = read_line(prompt)
    while not value:
        echo("  [!] this field is required")
        value = read_line(prompt)
    return value


def read_int(prompt, default):
    """Read an integer, fall back to default when the input is invalid."""
    text = read_line(prompt)
    if text.isdigit() and int(text) > 0:
        return int(text)
    return default


def confirm(question):
    """Ask a yes/no question, default is no."""
    answer = read_line(question + " [y/N]: ").lower()
    return answer in ("y", "yes")


def read_multiline(prompt):
    """Read several lines until a line that contains only END is entered."""
    echo("")
    echo(prompt)
    echo("  (finish with a line containing only " + END_MARKER + ")")
    collected = []
    while True:
        line = sys.stdin.readline()              # keep original spaces (code blocks)
        if line == "":                           # end of input (EOF)
            break
        if line.strip() == END_MARKER:           # terminator found
            break
        collected.append(line.rstrip("\n"))      # store line without newline
    return "\n".join(collected).strip()


# =========================================================================
# Key management
# =========================================================================

def parse_private_key(raw):
    """Accept nsec... or 64-char hex, return a PrivateKey object."""
    text = raw.strip()
    if text.startswith("nsec1"):                 # bech32 encoded private key
        return PrivateKey.from_nsec(text)
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):   # raw hex private key
        return PrivateKey.from_hex(text.lower())
    raise ValueError("unsupported key format: " + text[:16] + "...")


def hex_to_npub(hex_key):
    """Convert a hex public key to npub (falls back to hex if encoder differs)."""
    try:
        from nostr.bech32 import bech32_encode, convertbits  # bech32 encoder of the library
        data = convertbits(bytes.fromhex(hex_key), 8, 5)     # 8 bit groups -> 5 bit groups
        return bech32_encode("npub", data)                   # encode with npub prefix
    except Exception:
        return hex_key                                       # fallback: show raw hex


def active_account():
    """Return the currently selected private key."""
    if not accounts:
        raise RuntimeError("no private key loaded, use option 1 or 2 first")
    return accounts[current_index]


def active_npub():
    """Return the npub of the active account (or a placeholder)."""
    if not accounts:
        return "(no account)"
    return hex_to_npub(accounts[current_index].public_key().hex())


def save_keys_file():
    """Overwrite keys.txt with one nsec per line."""
    lines = [key.bech32() for key in accounts]
    KEY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_keys_file():
    """Load keys from keys.txt at program start."""
    if not KEY_FILE.exists():                    # first run, no file yet
        return
    known = {key.hex() for key in accounts}      # avoid duplicates
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):     # skip empty lines and comments
            continue
        try:
            key = parse_private_key(text)        # parse one key line
        except Exception as error:               # invalid line -> skip it
            echo("  [!] skip bad line in " + str(KEY_FILE) + ": " + str(error))
            continue
        if key.hex() not in known:               # keep every key only once
            accounts.append(key)
            known.add(key.hex())


def show_accounts():
    """List all accounts and let the user switch the active one."""
    global current_index
    if not accounts:
        echo("  [!] no key available, use option 1 or 2 first")
        return
    echo("")
    echo("--- accounts " + "-" * 41)
    for index, key in enumerate(accounts):
        marker = "*" if index == current_index else " "
        echo(" " + marker + " [" + str(index + 1) + "] " + hex_to_npub(key.public_key().hex()))
    echo("-" * 54)
    choice = read_line("Switch to number (Enter = keep current): ")
    if choice.isdigit() and 1 <= int(choice) <= len(accounts):
        current_index = int(choice) - 1          # switch active account
        echo("  active account -> #" + str(current_index + 1))


def batch_generate_keys():
    """Generate several new private keys and save them to keys.txt."""
    count = read_int("How many keys to generate? [" + str(DEFAULT_KEY_COUNT) + "]: ", DEFAULT_KEY_COUNT)
    created = []
    for _ in range(count):
        key = PrivateKey()                       # create a new random key pair
        accounts.append(key)                     # add to global account list
        created.append(key)                      # remember for output
    save_keys_file()                             # persist all keys
    echo("")
    echo("Generated " + str(count) + " key(s), saved to " + str(KEY_FILE))
    echo("")
    for index, key in enumerate(created, start=1):
        echo("  [" + str(index) + "] nsec : " + key.bech32())
        echo("       npub : " + key.public_key().bech32())
    echo("")
    echo("  [!] keep keys.txt safe and never commit it to git")


def batch_import_keys():
    """Import private keys from a file or by pasting them (one per line)."""
    source = read_line("Key file path (Enter = paste keys directly): ")
    if source:
        path = Path(source)
        if not path.exists():                    # file missing -> stop
            echo("  [!] file not found: " + str(path))
            return
        content = path.read_text(encoding="utf-8")           # read whole file
    else:
        content = read_multiline("Paste private keys (nsec or hex), one per line")

    imported = 0
    invalid = 0
    duplicated = 0
    known = {key.hex() for key in accounts}      # current keys for duplicate check
    for line in content.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):     # skip empty lines and comments
            continue
        try:
            key = parse_private_key(text)        # parse nsec or hex
        except Exception:
            invalid += 1                         # wrong format
            continue
        if key.hex() in known:                   # already loaded before
            duplicated += 1
            continue
        accounts.append(key)                     # store the imported key
        known.add(key.hex())
        imported += 1

    save_keys_file()                             # write updated key list to disk
    echo("")
    echo("  imported : " + str(imported))
    echo("  duplicate: " + str(duplicated))
    echo("  invalid  : " + str(invalid))
    echo("  total    : " + str(len(accounts)) + " keys in " + str(KEY_FILE))


# =========================================================================
# Event building and signing
# =========================================================================

def sign_event(private_key, kind, content, tags):
    """Build an event and sign it with the given private key."""
    event = Event(
        created_at=int(time.time()),             # unix timestamp (seconds)
        kind=kind,                               # 1 = note, 30023 = long article
        tags=tags,                               # list of [name, value, ...]
        content=content,                         # the actual text
        public_key=private_key.public_key().hex()  # author public key (hex)
    )
    event.id = event.hash()                      # compute event id
    private_key.sign_event(event)                # produce the signature
    return event


def make_identifier(title):
    """Build a stable d-tag identifier from the article title."""
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")   # ascii slug
    if len(ascii_slug) >= 3:                     # usable ascii identifier
        return ascii_slug[:48]
    return "post-" + str(int(time.time()))       # fallback for pure chinese titles


# =========================================================================
# Relay communication
# =========================================================================

def publish_events(events):
    """Send events to all relays in RELAYS and collect OK replies."""
    manager = RelayManager()                     # one manager per publish round
    for url in RELAYS:                           # register every custom relay
        manager.add_relay(url)
    manager.add_connections()                    # open websocket connections
    time.sleep(1.0)                              # wait for the handshake

    pending = {}                                 # events still waiting for OK
    for event in events:
        manager.publish_event(event)             # send EVENT message
        pending[event.id] = event                # remember what we sent
        time.sleep(PUBLISH_INTERVAL)             # rate limit between events

    accepted = {}                                # event id -> relays that said OK
    rejected = {}                                # event id -> error text
    deadline = time.time() + PUBLISH_ACK_TIMEOUT
    while pending and time.time() < deadline:    # wait until all relays answered
        while manager.message_pool.has_message():
            message = manager.message_pool.get_message()       # next relay answer
            event_id = getattr(message, "event_id", None)      # OK messages carry id
            if event_id not in pending:
                continue                                          # ignore other messages
            relay_url = getattr(message, "relay_url", "?")       # which relay replied
            if getattr(message, "success", False):               # relay accepted
                if event_id not in accepted:
                    accepted[event_id] = []
                accepted[event_id].append(relay_url)
            else:                                                # relay rejected
                reason = getattr(message, "msg", "rejected")
                rejected[event_id] = relay_url + ": " + str(reason)
        time.sleep(0.3)                                          # small polling gap

    try:
        manager.close_connections()              # close all websockets
    except Exception:
        pass                                     # shutdown problems are not fatal

    unanswered = [eid for eid in pending if eid not in accepted and eid not in rejected]
    return {"accepted": accepted, "rejected": rejected, "unanswered": unanswered}


def fetch_events(kinds, limit):
    """Fetch events from all relays in RELAYS, newest first."""
    manager = RelayManager()                     # one manager per fetch round
    for url in RELAYS:                           # register every custom relay
        manager.add_relay(url)
    subscription_id = "sub-fetch-1"              # name of this subscription
    relay_filter = Filter(kinds=kinds, limit=limit)               # what we ask for
    manager.add_subscription(subscription_id, relay_filter)
    manager.add_connections()                    # open websocket connections

    collected = {}                               # deduplicate by event id
    finished = False                             # becomes True on EOSE
    deadline = time.time() + FETCH_TIMEOUT
    while not finished and time.time() < deadline:
        while manager.message_pool.has_message():
            message = manager.message_pool.get_message()          # next relay answer
            event = getattr(message, "event", None)               # event message?
            if event is not None:
                collected[event.id] = event                       # store new event
            elif getattr(message, "subscription_id", None) == subscription_id:
                finished = True                                   # EOSE = relay done
        if not collected:                                          # no data yet
            time.sleep(0.25)                                       # avoid busy waiting

    try:
        manager.close_connections()              # close all websockets
    except Exception:
        pass                                     # shutdown problems are not fatal

    result = list(collected.values())                            # list of Event objects
    result.sort(key=lambda e: e.created_at, reverse=True)         # newest first
    return result


# =========================================================================
# Output helpers
# =========================================================================

def tag_value(event, name):
    """Return the first value of a tag, e.g. title / d / summary."""
    for tag in event.tags:
        if len(tag) >= 2 and tag[0] == name:
            return tag[1]
    return ""


def format_time(timestamp):
    """Format a unix timestamp as local time string."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def print_publish_report(report, events):
    """Show which event was accepted / rejected by which relay."""
    echo("")
    echo("--- publish report " + "-" * 40)
    for event in events:
        short_id = event.id[:16]                             # short id for display
        if event.id in report["accepted"]:
            relays = ", ".join(report["accepted"][event.id])  # relays that said OK
            echo("  [OK]      " + short_id + "... -> " + relays)
        elif event.id in report["rejected"]:
            echo("  [REJECT]  " + short_id + "... -> " + report["rejected"][event.id])
        else:
            echo("  [PENDING] " + short_id + "... -> no answer (may still be published)")
    summary = "  total=" + str(len(events))
    summary += " ok=" + str(len(report["accepted"]))
    summary += " rejected=" + str(len(report["rejected"]))
    summary += " pending=" + str(len(report["unanswered"]))
    echo(summary)


def print_event(event, index):
    """Print one fetched event in a readable form."""
    author = hex_to_npub(event.public_key)                   # author npub or hex
    title = tag_value(event, "title")                        # title tag of long article
    header = "#" + str(index) + " kind:" + str(event.kind)
    if title:
        header += " | " + title                              # append title when present
    echo("")
    echo("-" * 58)
    echo(header)
    echo("  id     : " + event.id)
    echo("  time   : " + format_time(event.created_at))
    echo("  author : " + author)
    body = event.content[:400]
    if len(event.content) > 400:
        body += "..."
    echo("  content: " + body)


# =========================================================================
# Menu actions
# =========================================================================

def publish_notes():
    """Publish several short notes, every message ends with END."""
    try:
        author_key = active_account()                        # key used for signing
    except RuntimeError as error:
        echo("  [!] " + str(error))
        return

    messages = []
    while True:                                              # collect messages
        prompt = "Message #" + str(len(messages) + 1) + " (empty END = stop sending)"
        text = read_multiline(prompt)
        if not text:
            break                                            # two END in a row = stop
        messages.append(text)

    if not messages:
        echo("  [!] nothing to send")
        return

    echo("")
    echo("--- preview " + "-" * 47)                          # show what will be sent
    for index, message in enumerate(messages, start=1):
        preview = message[:100].replace("\n", " ")
        suffix = "..." if len(message) > 100 else ""
        echo("  [" + str(index) + "] (" + str(len(message)) + " chars) " + preview + suffix)
    question = "Send " + str(len(messages)) + " message(s) to " + str(len(RELAYS)) + " relay(s)?"
    if not confirm(question):
        echo("  cancelled")
        return

    events = []
    for message in messages:
        event = sign_event(author_key, KIND_NOTE, message, [])   # sign each message
        events.append(event)
    report = publish_events(events)                          # send to relays
    print_publish_report(report, events)                     # show result


def publish_long_article():
    """Publish a long article (kind 30023). Title is required, content ends with END."""
    try:
        author_key = active_account()                        # key used for signing
    except RuntimeError as error:
        echo("  [!] " + str(error))
        return

    title = read_required("Article title (required): ")       # title must not be empty
    summary = read_line("Summary (optional, Enter to skip): ")  # optional description
    topics_text = read_line("Topics, comma separated (optional): ")
    content = read_multiline("Article content (markdown)")    # body ends with END

    if not content:
        echo("  [!] content is empty, nothing to publish")
        return

    identifier = make_identifier(title)                       # stable d-tag of article
    published_at = str(int(time.time()))                      # publish timestamp
    tags = []
    tags.append(["d", identifier])                            # identifier (same d = update)
    tags.append(["title", title])                             # required article title
    tags.append(["published_at", published_at])               # publish time
    if summary:
        tags.append(["summary", summary])                     # optional summary tag
    for topic in topics_text.split(","):                      # split "a,b,c"
        if topic.strip():
            clean_topic = topic.strip().lstrip("#").lower()
            tags.append(["t", clean_topic])                   # topic tag

    echo("")
    echo("  title      : " + title)
    echo("  identifier : " + identifier + "  (re-send same d = update article)")
    echo("  bytes      : " + str(len(content.encode("utf-8"))))
    if not confirm("Publish this long article?"):
        echo("  cancelled")
        return

    event = sign_event(author_key, KIND_LONGFORM, content, tags)  # sign the article
    report = publish_events([event])                          # send to relays
    print_publish_report(report, [event])                     # show result


def fetch_latest_events():
    """Fetch notes or long articles from all relays in RELAYS."""
    echo("  [1] short notes (kind 1)")
    echo("  [2] long articles (kind 30023)")
    mode = read_line("Choose type [1]: ") or "1"
    if mode == "2":
        kinds = [KIND_LONGFORM]                               # kind list for long articles
    else:
        kinds = [KIND_NOTE]                                   # kind list for notes
    limit = read_int("How many events? [" + str(DEFAULT_FETCH_LIMIT) + "]: ", DEFAULT_FETCH_LIMIT)

    echo("  fetching from: " + ", ".join(RELAYS))             # show target relays
    events = fetch_events(kinds, limit)                       # ask all relays
    if not events:
        echo("  [!] no events received")
        return

    echo("")
    echo("--- " + str(len(events)) + " event(s) " + "-" * 42)
    for index, event in enumerate(events, start=1):
        print_event(event, index)                             # print each event


def show_config():
    """Print relay list and key file information."""
    echo("")
    echo("--- configuration " + "-" * 42)
    echo("  relay list (edit RELAYS in nostr_cli.py):")
    for url in RELAYS:
        echo("    - " + url)
    echo("  keys file : " + str(KEY_FILE.resolve()))
    echo("  key count : " + str(len(accounts)))
    echo("  active    : " + active_npub())


# =========================================================================
# Main menu
# =========================================================================

MENU = """
============================================================
 Nostr Interactive CLI          account: {account} [{position}]
 relays: {relays}
============================================================
 [1] Batch generate private keys
 [2] Batch import private keys
 [3] List / switch accounts
 [4] Publish notes (batch, each message ends with END)
 [5] Publish long article (title required, content ends with END)
 [6] Fetch latest events
 [7] Show configuration
 [0] Exit
============================================================"""


def print_menu():
    """Render the menu with the active account and relay list."""
    if accounts:
        position = str(current_index + 1) + "/" + str(len(accounts))
    else:
        position = "0/0"
    text = MENU.format(account=active_npub(), position=position, relays=", ".join(RELAYS))
    echo(text)


def main():
    """Program entry: load keys, then run the menu loop."""
    load_keys_file()                                          # load keys.txt on start
    actions = {
        "1": batch_generate_keys,                             # batch key creation
        "2": batch_import_keys,                               # batch key import
        "3": show_accounts,                                   # list / switch accounts
        "4": publish_notes,                                   # batch short messages
        "5": publish_long_article,                            # long article with title
        "6": fetch_latest_events,                             # pull events from relays
        "7": show_config,                                     # show relay list / keys
    }
    while True:
        try:
            print_menu()                                      # show the menu
            choice = read_line("\nSelect option > ")          # read user choice
            if choice == "0":                                 # exit program
                echo("bye")
                return
            action = actions.get(choice)                      # map choice -> function
            if action is None:
                echo("  [!] unknown option")
                continue
            action()                                          # run the chosen action
            pause()                                           # wait before redraw
        except KeyboardInterrupt:                            # Ctrl+C anywhere
            echo("")
            echo("bye")
            return


if __name__ == "__main__":
    main()
