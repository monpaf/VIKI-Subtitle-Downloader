import argparse
import json
import hashlib
import math
import os
import re
import sys

import requests
from requests.exceptions import HTTPError

INFO = "https://api.viki.io/v4/containers/{pageid}.json"
SERIES = "https://api.viki.io/v4/containers/{pageid}/episodes.json"
SUBTITLE = "https://api.viki.io/v4/videos/{vid_id}/auth_subtitles/{lang}.srt"

HEADERS = {
    "Referer": "https://www.viki.com/",
    "X-Viki-App-Ver": "2.151.1",
    "X-Viki-Device-ID": "239083520d",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


class VIKI:
    def __init__(self, url: str, episode: str, language: str):
        match = re.search(r"/(tv|movies)/([0-9]{2,9}[vc])", url)
        if not match:
            raise ValueError("[-] Invalid URL.\nPlease use series or movie URL.")

        self.id = match.group(2)
        self.episode = episode
        self.language = language.lower()
        self._type = None
        self.app = "100000a"

        # Metadata for subtitles (per episode + language)
        self.metadata = {}
        self.meta_file = None

    # ----------------- Metadata helpers -----------------

    def _ensure_output_dir(self) -> str:
        output = os.path.join(os.getcwd(), "output")
        os.makedirs(output, exist_ok=True)
        return output

    def _load_metadata(self):
        """Load (or create) JSON metadata file before downloading subtitles."""
        output = self._ensure_output_dir()
        self.meta_file = os.path.join(output, "viki_subtitles.json")

        if not os.path.exists(self.meta_file):
            # Create empty JSON file
            with open(self.meta_file, "w", encoding="utf-8") as f:
                json.dump({}, f)
            self.metadata = {}
            return

        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
        except (json.JSONDecodeError, OSError):
            # If file is corrupted, start fresh
            self.metadata = {}

    def _save_metadata(self):
        if not self.meta_file:
            return
        with open(self.meta_file, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, ensure_ascii=False)

    def _get_old_entry(self, episode_title: str, lang: str):
        """Return previous metadata entry for this episode + language."""
        episode_meta = self.metadata.get(episode_title, {})
        return episode_meta.get(lang)

    def _update_entry(self, episode_title: str, lang: str, percent, checksum: str):
        """Update metadata entry for this episode + language."""
        episode_meta = self.metadata.get(episode_title, {})
        episode_meta[lang] = {"percent": percent, "checksum": checksum}
        self.metadata[episode_title] = episode_meta

    # ----------------- API helpers -----------------

    def print_language_completion(self, ep):
        lang = self.language.lower()
        if lang == "all":
            return

        subs = ep.get("subtitle_completions", {})
        pct = subs.get(lang)
        episode_index = ep.get("number")
        pct_display = f"{pct}%" if pct is not None else "N/A"
        print(f"{str(episode_index).ljust(5)} {pct_display.ljust(6)}")

    def get_titles(self):
        res = requests.get(
            url=INFO.format(pageid=self.id),
            params={"app": self.app},
            headers=HEADERS,
        )
        res = self.is_valid(res, "title")

        self._type = res.get("type")
        title = res.get("titles", {}).get("en")
        title_id = res.get("id") if self._type == "series" else res.get("watch_now", {}).get("id")

        total_episodes = (
            res.get("planned_episodes")
            if res.get("episodes", {}).get("count") == 0
            else res.get("episodes", {}).get("count")
        )

        print(f"[+] ID: {title_id}")
        print(f"[+] Title: {title}")
        print(f"[+] Type: {self._type}")

        # Movie / film
        if self._type in ["movie", "film"]:
            completions = res.get("subtitle_completions", {}) or {}
            return [
                {
                    "_id": title_id,
                    "title": title,
                    "completions": completions,
                    "subtitle": [
                        lang
                        for lang, percent in completions.items()
                        if percent is not None and percent > 90
                    ],
                }
            ]

        # Series
        titles = []
        i = 1
        while i < math.ceil(total_episodes / 50) + 1:
            vid = requests.get(
                url=SERIES.format(pageid=self.id),
                params={
                    "direction": "asc",
                    "with_upcoming": "true",
                    "sort": "number",
                    "blocked": "true",
                    "only_ids": "false",
                    "app": self.app,
                    "page": i,
                    "per_page": "50",
                },
                headers=HEADERS,
            )
            vid = self.is_valid(vid, "video list")
            i += 1

            for episode in vid.get("response", []):
                if not self.in_range(episode.get("number")):
                    continue

                self.print_language_completion(episode)
                completions = episode.get("subtitle_completions", {}) or {}
                titles.append(
                    {
                        "_id": episode.get("id"),
                        "title": title,
                        "episode": episode.get("number"),
                        "completions": completions,
                        "subtitle": [
                            lang
                            for lang, percent in completions.items()
                            if percent is not None and percent > 90
                        ],
                    }
                )
        return titles

    # ----------------- Subtitle download + change detection -----------------

    def get_subtitle(self):
        # Ensure JSON metadata exists and is loaded
        self._load_metadata()

        data = self.get_titles()

        downloaded_any = False
        language_missing_any = False

        for sub in data:
            # Build episode title used for filenames / metadata
            if self._type == "series":
                episode_num = sub.get("episode")
                episode_title = f"{sub.get('title')}.S01E{episode_num:02d}".replace(" ", ".")
                ep_label = f"episode {episode_num}"
            else:
                episode_title = sub.get("title").replace(" ", ".")
                ep_label = "movie"

            available_langs = sub.get("subtitle") or []

            # ⬇️ CHANGE: instead of raising, just warn and skip this episode
            if self.language != "all" and self.language not in available_langs:
                print(
                    f"[-] '{self.language}' subtitle is not available for {ep_label}."
                    f" Available: {available_langs}"
                )
                language_missing_any = True
                continue

            # For each available language, filter by requested language
            for lang in available_langs:
                if self.language != "all" and lang != self.language:
                    continue

                current_pct = sub.get("completions", {}).get(lang)

                # Fetch subtitle content first (in memory)
                content = self.fetch_subtitle_content(sub.get("_id"), lang)
                if content is None:
                    continue  # Already printed an error

                # Compute new checksum
                new_checksum = hashlib.md5(content).hexdigest()

                # Compare with stored metadata
                old_entry = self._get_old_entry(episode_title, lang)

                if old_entry is not None:
                    old_pct = old_entry.get("percent")
                    old_checksum = old_entry.get("checksum")

                    pct_changed = old_pct != current_pct
                    checksum_changed = old_checksum != new_checksum

                    if pct_changed or checksum_changed:
                        print(f"[*] Changes detected for {episode_title} [{lang}]:")
                        if pct_changed:
                            print(f"    - Completion: {old_pct}% -> {current_pct}%")
                        if checksum_changed:
                            print(f"    - Checksum:  {old_checksum} -> {new_checksum}")

                        answer = input(
                            "Do you still want to download this subtitle? [y/N]: "
                        ).strip().lower()
                        if answer not in ("y", "yes"):
                            print(
                                f"[-] Skipped download for {episode_title} "
                                f"language {lang} due to changes."
                            )
                            continue
                else:
                    # No previous metadata for this episode/lang
                    print(f"[*] First time download for {episode_title} [{lang}]")

                # Save subtitle to disk
                self.save_subtitle_file(episode_title, lang, content)

                # Update metadata (only after successful write)
                self._update_entry(episode_title, lang, current_pct, new_checksum)
                self._save_metadata()

                downloaded_any = True

        # Optional summary if nothing was downloaded for the requested language
        if not downloaded_any and language_missing_any and self.language != "all":
            print(
                f"[-] No subtitles were downloaded because '{self.language}' is not "
                f"available for the selected episodes."
            )


    def fetch_subtitle_content(self, sub_id, lang):
        """Fetch subtitle content (bytes) without writing to disk."""
        res = requests.get(
            url=SUBTITLE.format(vid_id=sub_id, lang=lang),
            params={"app": self.app},
            headers=HEADERS,
        )

        if res.status_code == 200:
            return res.content

        print(
            f"[-] Failed to download subtitle for video ID {sub_id}, "
            f"language {lang} (status {res.status_code})"
        )
        return None

    def save_subtitle_file(self, title, lang, content: bytes):
        output = self._ensure_output_dir()
        filename = os.path.join(output, f"{title}.{lang}.srt")
        with open(filename, "wb") as subtitle_file:
            subtitle_file.write(content)
        print(f"[+] Downloaded: {filename}")

    # ----------------- Utility methods -----------------

    def is_valid(self, res, stage):
        try:
            res.raise_for_status()
            return res.json()
        except HTTPError as e:
            raise HTTPError(f"[-] HTTP error during {stage}: {e}")
        except ValueError as e:
            raise ValueError(f"[-] JSON decode error during {stage}: {e}")

    def in_range(self, episode_num):
        if not self.episode:
            return True

        if "-" in self.episode:
            start, end = map(int, self.episode.split("-"))
            return start <= episode_num <= end

        if self.episode.isdigit():
            return episode_num == int(self.episode)

        return False


if __name__ == "__main__":
    parse = argparse.ArgumentParser(prog="VIKI subtitles download")
    parse.add_argument("url", type=str, help="viki url.")
    parse.add_argument(
        "-e",
        "--episode",
        type=str,
        default=None,
        help="Episode number (e.g., 1 or range 1-5)",
    )
    parse.add_argument(
        "-l",
        "--language",
        type=str,
        default="all",
        help="Subtitle language (e.g., en or all)",
    )
    args = parse.parse_args()

    start = VIKI(args.url, args.episode, args.language)

    try:
        start.get_subtitle()
    except ValueError as e:
        # Print a clean error message instead of a traceback
        print(f"[-] {e}")
        sys.exit(1)
