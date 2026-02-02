"""
SQLite persistent storage for Newsletter Advertiser Intelligence System.

Replaces JSON file-based storage with a proper database:
- Advertisers (company_name, domain, contacts, sector, etc.)
- Scanned issues tracking
- Retry queue for failed contact scrapes
- Custom domains (both company domains and newsletter domains)
- Newsletter sources for generic scanning
"""

import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "scraper_king.db"


class Database:
    """SQLite database for persistent storage."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS advertisers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                domain TEXT,
                sector TEXT DEFAULT 'other',
                sponsor_type TEXT,
                issue_url TEXT,
                issue_date TEXT,
                source TEXT DEFAULT 'scan',
                email_1 TEXT DEFAULT '',
                title_1 TEXT DEFAULT '',
                name_1 TEXT DEFAULT '',
                email_2 TEXT DEFAULT '',
                title_2 TEXT DEFAULT '',
                name_2 TEXT DEFAULT '',
                email_3 TEXT DEFAULT '',
                title_3 TEXT DEFAULT '',
                name_3 TEXT DEFAULT '',
                email_4 TEXT DEFAULT '',
                title_4 TEXT DEFAULT '',
                name_4 TEXT DEFAULT '',
                email_5 TEXT DEFAULT '',
                title_5 TEXT DEFAULT '',
                name_5 TEXT DEFAULT '',
                extra_data TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(domain)
            );

            CREATE TABLE IF NOT EXISTS scanned_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                newsletter_source TEXT,
                scanned_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                company_name TEXT NOT NULL,
                reason TEXT,
                attempts INTEGER DEFAULT 1,
                added TEXT NOT NULL,
                last_attempt TEXT NOT NULL,
                company_data TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS custom_domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                company_name TEXT,
                domain_type TEXT DEFAULT 'company',
                added TEXT NOT NULL,
                scanned INTEGER DEFAULT 0,
                scanned_at TEXT
            );

            CREATE TABLE IF NOT EXISTS newsletter_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                domain TEXT NOT NULL UNIQUE,
                archive_url TEXT,
                added TEXT NOT NULL,
                last_scanned TEXT,
                total_issues_found INTEGER DEFAULT 0,
                total_sponsors_found INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_advertisers_domain ON advertisers(domain);
            CREATE INDEX IF NOT EXISTS idx_scanned_issues_url ON scanned_issues(url);
            CREATE INDEX IF NOT EXISTS idx_retry_queue_domain ON retry_queue(domain);
            CREATE INDEX IF NOT EXISTS idx_custom_domains_domain ON custom_domains(domain);
            CREATE INDEX IF NOT EXISTS idx_newsletter_sources_domain ON newsletter_sources(domain);
        """)
        conn.commit()
        logger.info(f"Database initialized at {self.db_path}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ===== ADVERTISERS =====

    def get_advertisers(self, filters: dict | None = None) -> list[dict]:
        """Get all advertisers, optionally filtered."""
        conn = self._get_conn()
        query = "SELECT * FROM advertisers"
        params = []
        conditions = []

        if filters:
            if filters.get("has_email"):
                conditions.append("email_1 != ''")
            elif filters.get("has_email") is False:
                conditions.append("(email_1 = '' OR email_1 IS NULL)")
            if filters.get("sector"):
                conditions.append("sector = ?")
                params.append(filters["sector"])
            if filters.get("sponsor_type"):
                conditions.append("sponsor_type = ?")
                params.append(filters["sponsor_type"])
            if filters.get("source"):
                conditions.append("source = ?")
                params.append(filters["source"])

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY updated_at DESC"

        if filters and filters.get("limit"):
            query += " LIMIT ?"
            params.append(filters["limit"])
            if filters.get("offset"):
                query += " OFFSET ?"
                params.append(filters["offset"])

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_advertiser_dict(row) for row in rows]

    def get_advertiser_count(self, with_email: bool | None = None) -> int:
        """Get count of advertisers."""
        conn = self._get_conn()
        if with_email is True:
            return conn.execute("SELECT COUNT(*) FROM advertisers WHERE email_1 != ''").fetchone()[0]
        elif with_email is False:
            return conn.execute("SELECT COUNT(*) FROM advertisers WHERE email_1 = '' OR email_1 IS NULL").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM advertisers").fetchone()[0]

    def upsert_advertiser(self, data: dict) -> bool:
        """Insert or update an advertiser by domain. Returns True if new."""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        domain = (data.get("domain") or "").lower().strip()

        if not domain:
            # No domain — just insert with company name as key
            conn.execute("""
                INSERT INTO advertisers (company_name, domain, sector, sponsor_type,
                    issue_url, issue_date, source,
                    email_1, title_1, name_1, email_2, title_2, name_2,
                    email_3, title_3, name_3, email_4, title_4, name_4,
                    email_5, title_5, name_5, extra_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("company_name", "Unknown"), domain,
                data.get("sector", "other"), data.get("sponsor_type"),
                data.get("issue_url"), data.get("issue_date"),
                data.get("source", "scan"),
                data.get("email_1", ""), data.get("title_1", ""), data.get("name_1", ""),
                data.get("email_2", ""), data.get("title_2", ""), data.get("name_2", ""),
                data.get("email_3", ""), data.get("title_3", ""), data.get("name_3", ""),
                data.get("email_4", ""), data.get("title_4", ""), data.get("name_4", ""),
                data.get("email_5", ""), data.get("title_5", ""), data.get("name_5", ""),
                json.dumps(self._extract_extra(data)),
                now, now,
            ))
            conn.commit()
            return True

        # Check if exists
        existing = conn.execute("SELECT * FROM advertisers WHERE domain = ?", (domain,)).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO advertisers (company_name, domain, sector, sponsor_type,
                    issue_url, issue_date, source,
                    email_1, title_1, name_1, email_2, title_2, name_2,
                    email_3, title_3, name_3, email_4, title_4, name_4,
                    email_5, title_5, name_5, extra_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data.get("company_name", "Unknown"), domain,
                data.get("sector", "other"), data.get("sponsor_type"),
                data.get("issue_url"), data.get("issue_date"),
                data.get("source", "scan"),
                data.get("email_1", ""), data.get("title_1", ""), data.get("name_1", ""),
                data.get("email_2", ""), data.get("title_2", ""), data.get("name_2", ""),
                data.get("email_3", ""), data.get("title_3", ""), data.get("name_3", ""),
                data.get("email_4", ""), data.get("title_4", ""), data.get("name_4", ""),
                data.get("email_5", ""), data.get("title_5", ""), data.get("name_5", ""),
                json.dumps(self._extract_extra(data)),
                now, now,
            ))
            conn.commit()
            return True
        else:
            # Update: keep existing contacts if new data doesn't have them
            new_email = data.get("email_1", "")
            old_email = existing["email_1"] or ""

            # If new scan has no contacts but old did, preserve old contacts
            if not new_email and old_email:
                # Only update non-contact fields
                conn.execute("""
                    UPDATE advertisers SET
                        company_name = COALESCE(?, company_name),
                        sector = COALESCE(?, sector),
                        sponsor_type = COALESCE(?, sponsor_type),
                        updated_at = ?
                    WHERE domain = ?
                """, (
                    data.get("company_name"), data.get("sector"),
                    data.get("sponsor_type"), now, domain,
                ))
            else:
                conn.execute("""
                    UPDATE advertisers SET
                        company_name = COALESCE(?, company_name),
                        sector = COALESCE(?, sector),
                        sponsor_type = COALESCE(?, sponsor_type),
                        issue_url = COALESCE(?, issue_url),
                        issue_date = COALESCE(?, issue_date),
                        source = COALESCE(?, source),
                        email_1 = ?, title_1 = ?, name_1 = ?,
                        email_2 = ?, title_2 = ?, name_2 = ?,
                        email_3 = ?, title_3 = ?, name_3 = ?,
                        email_4 = ?, title_4 = ?, name_4 = ?,
                        email_5 = ?, title_5 = ?, name_5 = ?,
                        extra_data = ?,
                        updated_at = ?
                    WHERE domain = ?
                """, (
                    data.get("company_name"), data.get("sector"),
                    data.get("sponsor_type"), data.get("issue_url"),
                    data.get("issue_date"), data.get("source"),
                    data.get("email_1", ""), data.get("title_1", ""), data.get("name_1", ""),
                    data.get("email_2", ""), data.get("title_2", ""), data.get("name_2", ""),
                    data.get("email_3", ""), data.get("title_3", ""), data.get("name_3", ""),
                    data.get("email_4", ""), data.get("title_4", ""), data.get("name_4", ""),
                    data.get("email_5", ""), data.get("title_5", ""), data.get("name_5", ""),
                    json.dumps(self._extract_extra(data)),
                    now, domain,
                ))
            conn.commit()
            return False

    def bulk_upsert_advertisers(self, advertisers: list[dict]):
        """Upsert multiple advertisers."""
        for adv in advertisers:
            self.upsert_advertiser(adv)

    def clear_advertisers(self):
        """Clear all advertiser data."""
        conn = self._get_conn()
        conn.execute("DELETE FROM advertisers")
        conn.commit()

    def _row_to_advertiser_dict(self, row: sqlite3.Row) -> dict:
        """Convert a database row to the flat dict format used by the web app."""
        d = dict(row)
        # Remove internal fields
        d.pop("id", None)
        d.pop("created_at", None)
        d.pop("updated_at", None)
        # Merge extra_data
        extra = {}
        try:
            extra = json.loads(d.pop("extra_data", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        d.update(extra)
        return d

    @staticmethod
    def _extract_extra(data: dict) -> dict:
        """Extract fields that don't have dedicated columns into extra_data."""
        known_keys = {
            "company_name", "domain", "sector", "sponsor_type",
            "issue_url", "issue_date", "source",
            "email_1", "title_1", "name_1", "email_2", "title_2", "name_2",
            "email_3", "title_3", "name_3", "email_4", "title_4", "name_4",
            "email_5", "title_5", "name_5",
        }
        return {k: v for k, v in data.items() if k not in known_keys and v is not None}

    # ===== SCANNED ISSUES =====

    def is_issue_scanned(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM scanned_issues WHERE url = ?", (url,)).fetchone()
        return row is not None

    def mark_issue_scanned(self, url: str, newsletter_source: str | None = None):
        conn = self._get_conn()
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO scanned_issues (url, newsletter_source, scanned_at) VALUES (?, ?, ?)",
            (url, newsletter_source, now),
        )
        conn.commit()

    def get_scanned_issues(self) -> set[str]:
        conn = self._get_conn()
        rows = conn.execute("SELECT url FROM scanned_issues").fetchall()
        return {row["url"] for row in rows}

    def get_scanned_issues_count(self) -> int:
        conn = self._get_conn()
        return conn.execute("SELECT COUNT(*) FROM scanned_issues").fetchone()[0]

    def clear_scanned_issues(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM scanned_issues")
        conn.commit()

    # ===== RETRY QUEUE =====

    def get_retry_queue(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM retry_queue ORDER BY last_attempt DESC").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d.pop("id", None)
            try:
                d["company_data"] = json.loads(d.get("company_data", "{}") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["company_data"] = {}
            result.append(d)
        return result

    def add_to_retry_queue(self, domain: str, company_name: str, reason: str, company_data: dict | None = None):
        conn = self._get_conn()
        now = datetime.now().isoformat()
        existing = conn.execute("SELECT * FROM retry_queue WHERE domain = ?", (domain,)).fetchone()

        if existing:
            conn.execute("""
                UPDATE retry_queue SET
                    attempts = attempts + 1,
                    reason = ?,
                    last_attempt = ?,
                    company_data = ?
                WHERE domain = ?
            """, (reason, now, json.dumps(company_data or {}), domain))
        else:
            conn.execute("""
                INSERT INTO retry_queue (domain, company_name, reason, attempts, added, last_attempt, company_data)
                VALUES (?, ?, ?, 1, ?, ?, ?)
            """, (domain, company_name, reason, now, now, json.dumps(company_data or {})))
        conn.commit()

    def remove_from_retry_queue(self, domain: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM retry_queue WHERE domain = ?", (domain,))
        conn.commit()

    def clear_retry_queue(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM retry_queue")
        conn.commit()

    # ===== CUSTOM DOMAINS =====

    def get_custom_domains(self, domain_type: str | None = None) -> list[dict]:
        conn = self._get_conn()
        if domain_type:
            rows = conn.execute(
                "SELECT * FROM custom_domains WHERE domain_type = ? ORDER BY added DESC",
                (domain_type,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM custom_domains ORDER BY added DESC").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d.pop("id", None)
            d["scanned"] = bool(d.get("scanned", 0))
            result.append(d)
        return result

    def add_custom_domain(self, domain: str, company_name: str | None = None,
                          domain_type: str = "company") -> bool:
        """Add a custom domain. Returns True if added, False if already exists."""
        conn = self._get_conn()
        # Clean domain
        if domain.startswith(("http://", "https://")):
            from urllib.parse import urlparse
            domain = urlparse(domain).netloc
        if domain.startswith("www."):
            domain = domain[4:]

        try:
            conn.execute(
                "INSERT INTO custom_domains (domain, company_name, domain_type, added) VALUES (?, ?, ?, ?)",
                (domain, company_name or domain.split(".")[0].title(), domain_type,
                 datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_custom_domain(self, domain: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM custom_domains WHERE domain = ?", (domain,))
        conn.commit()

    def mark_custom_domain_scanned(self, domain: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE custom_domains SET scanned = 1, scanned_at = ? WHERE domain = ?",
            (datetime.now().isoformat(), domain),
        )
        conn.commit()

    # ===== NEWSLETTER SOURCES =====

    def get_newsletter_sources(self, active_only: bool = True) -> list[dict]:
        conn = self._get_conn()
        query = "SELECT * FROM newsletter_sources"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY name"
        rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]

    def add_newsletter_source(self, name: str, domain: str, archive_url: str | None = None) -> bool:
        """Add a newsletter source. Returns True if added."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO newsletter_sources (name, domain, archive_url, added) VALUES (?, ?, ?, ?)",
                (name, domain, archive_url, datetime.now().isoformat()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def update_newsletter_source(self, domain: str, **kwargs):
        conn = self._get_conn()
        sets = []
        params = []
        for key, val in kwargs.items():
            if key in ("last_scanned", "total_issues_found", "total_sponsors_found", "active", "archive_url", "name"):
                sets.append(f"{key} = ?")
                params.append(val)
        if sets:
            params.append(domain)
            conn.execute(f"UPDATE newsletter_sources SET {', '.join(sets)} WHERE domain = ?", params)
            conn.commit()

    def remove_newsletter_source(self, domain: str):
        conn = self._get_conn()
        conn.execute("DELETE FROM newsletter_sources WHERE domain = ?", (domain,))
        conn.commit()

    # ===== MIGRATION: Import from JSON files =====

    def import_from_json(self, output_dir: Path):
        """Import existing JSON data into the database."""
        # Import advertisers
        data_file = output_dir / "latest_scan.json"
        if data_file.exists():
            try:
                with open(data_file) as f:
                    data = json.load(f)
                advertisers = data.get("advertisers", [])
                if advertisers:
                    self.bulk_upsert_advertisers(advertisers)
                    logger.info(f"Imported {len(advertisers)} advertisers from JSON")
            except Exception as e:
                logger.error(f"Failed to import advertisers: {e}")

        # Import scanned issues
        issues_file = output_dir / "scanned_issues.json"
        if issues_file.exists():
            try:
                with open(issues_file) as f:
                    data = json.load(f)
                issues = data.get("issues", [])
                for url in issues:
                    self.mark_issue_scanned(url)
                logger.info(f"Imported {len(issues)} scanned issues from JSON")
            except Exception as e:
                logger.error(f"Failed to import scanned issues: {e}")

        # Import retry queue
        retry_file = output_dir / "retry_queue.json"
        if retry_file.exists():
            try:
                with open(retry_file) as f:
                    queue = json.load(f)
                for item in queue:
                    self.add_to_retry_queue(
                        item["domain"], item["company_name"],
                        item.get("reason", ""), item.get("company_data"),
                    )
                logger.info(f"Imported {len(queue)} retry queue items from JSON")
            except Exception as e:
                logger.error(f"Failed to import retry queue: {e}")

        # Import custom domains
        domains_file = output_dir / "custom_domains.json"
        if domains_file.exists():
            try:
                with open(domains_file) as f:
                    domains = json.load(f)
                for d in domains:
                    self.add_custom_domain(d["domain"], d.get("company_name"))
                    if d.get("scanned"):
                        self.mark_custom_domain_scanned(d["domain"])
                logger.info(f"Imported {len(domains)} custom domains from JSON")
            except Exception as e:
                logger.error(f"Failed to import custom domains: {e}")

    # ===== STATS =====

    def get_stats(self) -> dict:
        """Get overall database statistics."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM advertisers").fetchone()[0]
        with_email = conn.execute("SELECT COUNT(*) FROM advertisers WHERE email_1 != ''").fetchone()[0]
        scanned = conn.execute("SELECT COUNT(*) FROM scanned_issues").fetchone()[0]
        retry = conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()[0]
        newsletters = conn.execute("SELECT COUNT(*) FROM newsletter_sources WHERE active = 1").fetchone()[0]

        # Group by sector
        categories = {}
        for row in conn.execute("SELECT sector, COUNT(*) as cnt FROM advertisers GROUP BY sector").fetchall():
            categories[row["sector"] or "other"] = row["cnt"]

        # Group by sponsor type
        sources = {}
        for row in conn.execute("SELECT sponsor_type, COUNT(*) as cnt FROM advertisers GROUP BY sponsor_type").fetchall():
            sources[row["sponsor_type"] or "unknown"] = row["cnt"]

        return {
            "total": total,
            "with_emails": with_email,
            "without_emails": total - with_email,
            "scanned_issues": scanned,
            "retry_queue": retry,
            "newsletter_sources": newsletters,
            "categories": categories,
            "sources": sources,
        }
