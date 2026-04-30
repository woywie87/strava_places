import json
import os
import secrets
import sys
import threading
import time
import tkinter as tk
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from tkinter import filedialog, messagebox, ttk
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from strava_core import (
    AUTH_URL,
    PLACE_NODE_MATCH_RADIUS_METERS,
    activity_to_row,
    build_places_csv,
    exchange_code_for_token,
    extract_places_from_activity,
    fetch_activities_page,
)


ACTIVITIES_PAGE_SIZE = 20
STRAVA_API_PAGE_SIZE = 20
STATUS_DEFAULT_COLOR = "#374151"
STATUS_PROGRESS_COLOR = "#2563eb"
STATUS_SUCCESS_COLOR = "#15803d"
STATUS_ERROR_COLOR = "#dc2626"

DESKTOP_STRAVA_CONFIG_FILENAME = "strava_desktop_config.json"


def desktop_strava_config_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), DESKTOP_STRAVA_CONFIG_FILENAME)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DESKTOP_STRAVA_CONFIG_FILENAME)


def load_desktop_strava_config():
    path = desktop_strava_config_path()
    if not os.path.isfile(path):
        return "", "", ""
    try:
        with open(path, encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    callback_url = str(data.get("callback_url") or "").strip()
    return client_id, client_secret, callback_url


class DesktopOAuthCallbackHandler(BaseHTTPRequestHandler):
    server_version = "StravaDesktopOAuth/1.0"

    def do_GET(self):
        parsed_url = urlparse(self.path)
        query = parse_qs(parsed_url.query)

        if parsed_url.path != self.server.expected_callback_path:
            self.send_error(404, "Not found")
            return

        if query.get("state", [""])[0] != self.server.expected_state:
            self.send_error(400, "Invalid OAuth state")
            return

        if "error" in query:
            self.server.oauth_error = query["error"][0]
            self._send_text("Logowanie do Stravy zostalo anulowane albo odrzucone.")
            return

        code = query.get("code", [""])[0]
        if not code:
            self.send_error(400, "Missing authorization code")
            return

        self.server.authorization_code = code
        self._send_text("Logowanie zakonczone. Mozesz wrocic do aplikacji.")

    def log_message(self, format, *args):
        return

    def _send_text(self, message):
        body = message.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_callback_url(callback_url):
    parsed_url = urlparse(callback_url)
    if parsed_url.scheme != "http":
        raise ValueError("Desktopowy callback musi uzywac schematu http.")
    if not parsed_url.hostname:
        raise ValueError("Podaj pelny adres callbacku (http, host, port i sciezka).")

    callback_path = parsed_url.path or "/callback"
    port = parsed_url.port or 80
    bind_host = parsed_url.hostname

    return bind_host, port, callback_path, callback_url


def get_authorization_code(client_id, callback_url):
    bind_host, port, callback_path, redirect_uri = parse_callback_url(callback_url)
    state = secrets.token_urlsafe(24)
    server = HTTPServer((bind_host, port), DesktopOAuthCallbackHandler)
    server.expected_state = state
    server.expected_callback_path = callback_path
    server.authorization_code = None
    server.oauth_error = None

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": state,
    }
    webbrowser.open(f"{AUTH_URL}?{urlencode(params)}")

    try:
        while server.authorization_code is None and server.oauth_error is None:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    if server.oauth_error:
        raise RuntimeError(f"Strava zwrocila blad OAuth: {server.oauth_error}")

    return server.authorization_code


class DesktopApp:
    def __init__(self, root):
        self.root = root
        self.access_token = None
        self.activities = {}
        self.current_page = 1
        self.has_next_page = False
        self.loaded_pages = {}
        self.loaded_page_has_next = {}
        self.is_loading_activities = False
        self.request_id = 0

        self.client_id_var = tk.StringVar()
        self.client_secret_var = tk.StringVar()
        self.callback_url_var = tk.StringVar()
        self.radius_var = tk.IntVar(value=PLACE_NODE_MATCH_RADIUS_METERS)
        self.status_var = tk.StringVar(value="Wpisz dane aplikacji Strava i zaloguj sie.")
        self.page_var = tk.StringVar(value="Strona 0")

        file_client_id, file_client_secret, file_callback_url = load_desktop_strava_config()
        self.client_id_var.set(file_client_id)
        self.client_secret_var.set(file_client_secret)
        self.callback_url_var.set(file_callback_url)

        self.root.title("Strava Places Parser")
        self.root.geometry("1100x620")
        self.root.minsize(900, 500)
        self.create_widgets()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding=14)
        main_frame.pack(fill=tk.BOTH, expand=True)

        login_frame = ttk.LabelFrame(main_frame, text="Strava API", padding=12)
        login_frame.pack(fill=tk.X)
        login_frame.columnconfigure(1, weight=1)

        ttk.Label(login_frame, text="Client ID").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        ttk.Entry(login_frame, textvariable=self.client_id_var).grid(row=0, column=1, sticky=tk.EW)

        ttk.Label(login_frame, text="Client Secret").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=(8, 0))
        ttk.Entry(login_frame, textvariable=self.client_secret_var, show="*").grid(row=1, column=1, sticky=tk.EW, pady=(8, 0))

        ttk.Label(login_frame, text="Callback URL").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=(8, 0))
        ttk.Entry(login_frame, textvariable=self.callback_url_var).grid(row=2, column=1, sticky=tk.EW, pady=(8, 0))

        self.login_button = ttk.Button(login_frame, text="Zaloguj przez Strave", command=self.login)
        self.login_button.grid(row=0, column=2, rowspan=3, padx=(12, 0), sticky=tk.NSEW)

        filters_frame = ttk.Frame(main_frame)
        filters_frame.pack(fill=tk.X, pady=(14, 0))

        ttk.Label(filters_frame, text="Promien punktow OSM [m]").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        ttk.Spinbox(
            filters_frame,
            from_=0,
            to=5000,
            increment=50,
            textvariable=self.radius_var,
            width=8,
        ).grid(row=0, column=1, sticky=tk.W)

        table_frame = ttk.Frame(main_frame)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(14, 0))

        columns = ("date", "type", "distance", "duration", "name")
        self.table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=12,
        )
        self.table.heading("date", text="Data")
        self.table.heading("type", text="Typ")
        self.table.heading("distance", text="Dystans")
        self.table.heading("duration", text="Czas")
        self.table.heading("name", text="Nazwa")
        self.table.column("date", width=100, anchor=tk.CENTER)
        self.table.column("type", width=120, anchor=tk.CENTER)
        self.table.column("distance", width=100, anchor=tk.E)
        self.table.column("duration", width=100, anchor=tk.E)
        self.table.column("name", width=560, anchor=tk.W)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.table_scrollbar = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=self.on_table_scrollbar,
        )
        self.table_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.table.configure(yscrollcommand=self.table_scrollbar.set)

        actions_frame = ttk.Frame(main_frame)
        actions_frame.pack(fill=tk.X, pady=(12, 0))

        self.previous_button = ttk.Button(
            actions_frame,
            text="Poprzednia",
            command=self.previous_page,
            state=tk.DISABLED,
        )
        self.previous_button.pack(side=tk.LEFT, padx=(0, 8))

        self.next_button = ttk.Button(
            actions_frame,
            text="Nastepna",
            command=self.next_page,
            state=tk.DISABLED,
        )
        self.next_button.pack(side=tk.LEFT)

        ttk.Label(actions_frame, textvariable=self.page_var).pack(side=tk.LEFT)

        self.export_button = ttk.Button(
            actions_frame,
            text="Eksportuj miejscowosci",
            command=self.export_selected_activity,
            state=tk.DISABLED,
        )
        self.export_button.pack(side=tk.RIGHT)

        self.status_label = ttk.Label(
            main_frame,
            textvariable=self.status_var,
            font=("Segoe UI", 11, "bold"),
        )
        self.status_label.pack(fill=tk.X, pady=(10, 0))

    def set_status(self, message, color=STATUS_DEFAULT_COLOR):
        self.status_var.set(message)
        self.status_label.configure(foreground=color)

    def login(self):
        client_id = self.client_id_var.get().strip()
        client_secret = self.client_secret_var.get().strip()
        callback_url = self.callback_url_var.get().strip()
        if not client_id or not client_secret:
            messagebox.showerror("Brak danych", "Podaj Client ID i Client Secret.")
            return
        if not callback_url:
            messagebox.showerror(
                "Brak callbacku",
                "Podaj Callback URL w polu albo w pliku strava_desktop_config.json (klucz callback_url).",
            )
            return
        try:
            parse_callback_url(callback_url)
        except ValueError as error:
            messagebox.showerror("Niepoprawny callback", str(error))
            return

        self.login_button.configure(state=tk.DISABLED)
        self.set_status("Otwieram logowanie Stravy...", STATUS_PROGRESS_COLOR)

        thread = threading.Thread(
            target=self.login_worker,
            args=(client_id, client_secret, callback_url),
            daemon=True,
        )
        thread.start()

    def login_worker(self, client_id, client_secret, callback_url):
        session = {"client_id": client_id, "client_secret": client_secret}
        try:
            code = get_authorization_code(client_id, callback_url)
            self.access_token = exchange_code_for_token(code, session)
            self.root.after(0, self.reset_and_load_activities)
        except OSError as error:
            self.root.after(
                0,
                self.show_error,
                f"Nie udalo sie uruchomic callbacku pod adresem {callback_url}. Zamknij proces uzywajacy tego portu albo ustaw inny callback URL. Szczegoly: {error}",
            )
        except (RuntimeError, requests.RequestException) as error:
            self.root.after(0, self.show_error, f"Logowanie nieudane: {error}")

    def show_activities(self, page):
        self.login_button.configure(state=tk.NORMAL)
        self.is_loading_activities = False
        self.current_page = page
        self.has_next_page = self.loaded_page_has_next.get(page, False)
        self.render_current_page()
        self.set_status("Gotowe.", STATUS_SUCCESS_COLOR)

    def show_error(self, message):
        self.is_loading_activities = False
        self.login_button.configure(state=tk.NORMAL)
        self.export_button.configure(state=tk.NORMAL if self.activities else tk.DISABLED)
        self.set_status(message, STATUS_ERROR_COLOR)
        messagebox.showerror("Blad", message)

    def reset_and_load_activities(self):
        self.loaded_pages = {}
        self.loaded_page_has_next = {}
        self.activities = {}
        self.current_page = 1
        self.has_next_page = False
        self.table.delete(*self.table.get_children())
        self.page_var.set("Strona 0")
        self.export_button.configure(state=tk.DISABLED)
        self.load_activities_page(1)

    def load_activities_page(self, page):
        if not self.access_token or self.is_loading_activities:
            return

        if page in self.loaded_pages:
            self.current_page = page
            self.has_next_page = self.loaded_page_has_next.get(page, False)
            self.render_current_page()
            return

        self.previous_button.configure(state=tk.DISABLED)
        self.next_button.configure(state=tk.DISABLED)
        self.export_button.configure(state=tk.DISABLED)
        self.is_loading_activities = True
        self.set_status("Pobieram aktywnosci ze Stravy...", STATUS_PROGRESS_COLOR)

        self.request_id += 1
        request_id = self.request_id
        thread = threading.Thread(
            target=self.load_activities_page_worker,
            args=(page, request_id),
            daemon=True,
        )
        thread.start()

    def load_activities_page_worker(self, api_page, request_id):
        try:
            api_activities = fetch_activities_page(
                self.access_token,
                page=api_page,
                per_page=STRAVA_API_PAGE_SIZE,
            )
            if request_id != self.request_id:
                return

            has_next_page = len(api_activities) == STRAVA_API_PAGE_SIZE
            self.root.after(0, self.finish_page_load, api_page, request_id, api_activities, has_next_page)
        except requests.RequestException as error:
            if request_id == self.request_id:
                self.root.after(0, self.show_error, f"Nie udalo sie pobrac aktywnosci: {error}")

    def finish_page_load(self, api_page, request_id, activities, has_next_page):
        if request_id != self.request_id:
            return
        self.loaded_pages[api_page] = activities
        self.loaded_page_has_next[api_page] = has_next_page
        self.show_activities(api_page)

    def render_current_page(self):
        self.table.delete(*self.table.get_children())
        visible_activities = self.loaded_pages.get(self.current_page, [])
        self.activities = {str(activity["id"]): activity for activity in visible_activities}

        for activity in visible_activities:
            row = activity_to_row(activity)
            self.table.insert(
                "",
                tk.END,
                iid=row["id"],
                values=(
                    row["date"],
                    row["type"],
                    row["distance"],
                    row["duration"],
                    row["name"],
                ),
            )

        loaded_count = sum(len(self.loaded_pages[page]) for page in sorted(self.loaded_pages) if page <= self.current_page)
        if visible_activities:
            self.page_var.set(f"Strona {self.current_page} ({loaded_count} aktywnosci)")
        else:
            self.page_var.set(f"Strona {self.current_page} (brak wynikow)")

        self.previous_button.configure(state=tk.NORMAL if self.current_page > 1 else tk.DISABLED)
        can_show_next = (self.current_page + 1) in self.loaded_pages
        can_load_next = self.has_next_page
        self.next_button.configure(state=tk.NORMAL if can_show_next or can_load_next else tk.DISABLED)
        self.export_button.configure(state=tk.NORMAL if visible_activities else tk.DISABLED)

    def on_table_scrollbar(self, *args):
        self.table.yview(*args)

    def previous_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self.has_next_page = self.loaded_page_has_next.get(self.current_page, False)
            self.render_current_page()

    def next_page(self):
        if (self.current_page + 1) in self.loaded_pages or self.has_next_page:
            self.load_activities_page(self.current_page + 1)

    def export_selected_activity(self):
        if not self.access_token:
            messagebox.showerror("Brak logowania", "Najpierw zaloguj sie do Stravy.")
            return

        selected_ids = self.table.selection()
        if not selected_ids:
            messagebox.showinfo("Brak wyboru", "Najpierw zaznacz jedna aktywnosc.")
            return

        activity_id = selected_ids[0]
        activity = self.activities[activity_id]
        path = filedialog.asksaveasfilename(
            title="Zapisz miejscowosci",
            defaultextension=".csv",
            initialfile=f"miejscowosci_{activity_id}.csv",
            filetypes=[("CSV", "*.csv"), ("Wszystkie pliki", "*.*")],
        )
        if not path:
            return

        self.export_button.configure(state=tk.DISABLED)
        self.set_status("Eksportuje miejscowosci...", STATUS_PROGRESS_COLOR)

        thread = threading.Thread(
            target=self.export_worker,
            args=(activity_id, activity, path, self.radius_var.get()),
            daemon=True,
        )
        thread.start()

    def export_worker(self, activity_id, activity, path, radius_meters):
        try:
            places = extract_places_from_activity(
                self.access_token,
                activity_id,
                radius_meters,
            )
            csv_content = build_places_csv(activity_id, activity, places)
            with open(path, "w", newline="", encoding="utf-8-sig") as csv_file:
                csv_file.write(csv_content)

            self.root.after(0, self.finish_export, f"Zapisano {len(places)} miejscowosci do {path}")
        except (OSError, requests.RequestException) as error:
            self.root.after(0, self.show_error, f"Eksport nieudany: {error}")

    def finish_export(self, message):
        self.export_button.configure(state=tk.NORMAL)
        self.set_status(message, STATUS_SUCCESS_COLOR)
        messagebox.showinfo("Eksport gotowy", message)


def main():
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
