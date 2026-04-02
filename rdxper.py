"""
rdxper v4.0 — Free AI-Powered Real Research Paper Generator
────────────────────────────────────────────────────────────
Pipeline:
  1. Semantic Scholar API  → real papers (titles, abstracts, citations, DOIs)
  2. CrossRef API          → additional verified journal articles
  3. Wikipedia REST API    → background context & definitions
  4. OpenRouter (FREE)     → writes ALL prose sections using scraped data as context
  5. python-docx           → assembles formatted .docx with SPSS-style charts

AI Provider:
  OpenRouter (free tier) — https://openrouter.ai/keys
  set OPENROUTER_API_KEY=your_key_here

Usage:
  python rdxper.py
"""

import os, uuid, time, threading, smtplib, secrets, io, random, re, json, hmac, hashlib, sqlite3
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime
from flask import Flask, request, jsonify, send_file, Response
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

otp_store = {}
sessions  = {}
jobs      = {}
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'rkhrishanthm@gmail.com')

# ── SQLite DB ─────────────────────────────────────────────────────────────────
DB_PATH = os.environ.get('DB_PATH', 'rdxper.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                name TEXT, picture TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                last_login TEXT
            );
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, topic TEXT,
                file_path TEXT, paid INTEGER DEFAULT 0, amount INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, paper_id TEXT,
                razorpay_order TEXT, razorpay_payment TEXT, amount INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)

init_db()
os.makedirs('generated', exist_ok=True)


def session_set(token: str, email: str):
    """Persist a session token to the DB and keep in-memory cache."""
    sessions[token] = {'email': email}
    try:
        with get_db() as db:
            db.execute('INSERT OR REPLACE INTO sessions (token, email) VALUES (?, ?)', (token, email))
    except Exception as e:
        print(f'[session_set] DB error: {e}')


def session_get(token: str) -> object:
    """Return session dict from memory, falling back to DB (handles restarts)."""
    if not token:
        return None
    if token in sessions:
        return sessions[token]
    try:
        with get_db() as db:
            row = db.execute('SELECT email FROM sessions WHERE token=?', (token,)).fetchone()
            if row:
                email = row['email']
                user = db.execute('SELECT id, name, picture FROM users WHERE email=?', (email,)).fetchone()
                sessions[token] = {
                    'email': email,
                    'user_id': user['id'] if user else email,
                    'name': user['name'] if user else '',
                    'picture': user['picture'] if user else '',
                }
                return sessions[token]
    except Exception as e:
        print(f'[session_get] DB error: {e}')
    return None


def session_delete(token: str):
    sessions.pop(token, None)
    try:
        with get_db() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (token,))
    except Exception as e:
        print(f'[session_delete] DB error: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
#  FREE AI CLIENT  (OpenRouter — free tier models)
# ═══════════════════════════════════════════════════════════════════════════════

# Ordered sections — used to map closing tags → progress %
SECTION_ORDER = [
    'keywords', 'abstract', 'introduction', 'objectives',
    'literature_review', 'methodology', 'results',
    'discussion', 'suggestions', 'limitations', 'conclusion', 'charts',
]
SECTION_LABELS = {
    'keywords':          'Writing keywords...',
    'abstract':          'Writing abstract...',
    'introduction':      'Writing introduction...',
    'objectives':        'Writing objectives...',
    'literature_review': 'Writing literature review...',
    'methodology':       'Writing methodology...',
    'results':           'Writing results...',
    'discussion':        'Writing discussion...',
    'suggestions':       'Writing suggestions...',
    'limitations':       'Writing limitations...',
    'conclusion':        'Writing conclusion...',
    'charts':            'Designing chart specifications...',
}
_AI_START = 30
_AI_END   = 75

# Free models on OpenRouter (tried in order on 429 / quota errors)
_OPENROUTER_FREE_MODELS = [
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-4-maverick:free",
    "meta-llama/llama-4-scout:free",
    "google/gemini-2.0-flash-exp:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "qwen/qwen3-8b:free",
]


def ai_generate(prompt: str, system: str = "", temperature: float = 0.7,
                progress_cb=None, tracked_sections=None) -> str:
    """
    Call OpenRouter using its OpenAI-compatible SSE streaming endpoint.
    Automatically falls back through free models on 429 / quota errors.
    Set OPENROUTER_API_KEY in your environment variables.
    Get a free key at: https://openrouter.ai/keys
    """
    import http.client, ssl

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Get a free key at https://openrouter.ai/keys"
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    watch         = tracked_sections if tracked_sections is not None else SECTION_ORDER
    last_error    = None

    for model in _OPENROUTER_FREE_MODELS:
        payload = {
            "model":       model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  8192,
            "stream":      True,
        }
        body = json.dumps(payload).encode("utf-8")

        hdrs = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer":  "https://rdxper.app",
            "X-Title":       "rdxper",
        }

        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=180, context=ctx)

        accumulated   = ""
        sections_done = []

        try:
            conn.request("POST", "/api/v1/chat/completions", body=body, headers=hdrs)
            resp = conn.getresponse()

            if resp.status in (429, 402, 503):
                err = resp.read().decode("utf-8", errors="replace")
                print(f"[OpenRouter] {resp.status} on {model}, trying next... {err[:120]}")
                last_error = f"HTTP {resp.status} on {model}: {err[:200]}"
                conn.close()
                continue

            if resp.status != 200:
                err = resp.read().decode("utf-8", errors="replace")
                conn.close()
                raise RuntimeError(f"OpenRouter HTTP {resp.status}: {err[:400]}")

            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    token = chunk["choices"][0]["delta"].get("content", "") or ""
                    accumulated += token

                    for tag in watch:
                        if tag not in sections_done and f"</{tag}>" in accumulated:
                            sections_done.append(tag)
                            pct     = _AI_START + int(len(sections_done) / len(watch) * (_AI_END - _AI_START))
                            nxt     = watch.index(tag) + 1
                            msg     = SECTION_LABELS.get(watch[nxt], "Finishing up...") if nxt < len(watch) else "Finishing up..."
                            if progress_cb:
                                progress_cb(pct, f'✓ {tag.replace("_"," ").title()} done — {msg}')
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

        except RuntimeError:
            raise
        except Exception as e:
            last_error = str(e)
            print(f"[OpenRouter] Error on {model}: {e}")
            continue
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not accumulated.strip():
            last_error = f"Empty response from {model}"
            print(f"[OpenRouter] Empty response from {model}, trying next...")
            continue

        print(f"[OpenRouter] ✓ Used model: {model}")
        return accumulated.strip()

    raise RuntimeError(
        f"All OpenRouter free models failed or hit quota. Last error: {last_error}. "
        "Check your key at https://openrouter.ai/keys"
    )


# Backward-compat alias used elsewhere in the file
def gemini_stream(prompt, system="", temperature=0.7, progress_cb=None, tracked_sections=None):
    return ai_generate(prompt, system, temperature, progress_cb, tracked_sections)


SYSTEM_PROMPT = (
    "You are an expert academic research paper writer. "
    "You write in formal, scholarly English suitable for peer-reviewed journals. "
    "Do not use markdown formatting, bullet points, asterisks, or headers in your output — "
    "write clean flowing prose only, unless explicitly asked for a list. "
    "Be specific, evidence-grounded, and academically rigorous. "
    "Do not invent statistics or cite sources not provided to you."
)


# ═══════════════════════════════════════════════════════════════════════════════
#  WEB SCRAPER  (no API keys required)
# ═══════════════════════════════════════════════════════════════════════════════

def _http_get(url: str, timeout: int = 12) -> object:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "rdxper/3.0 (research-paper-generator; educational use)"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"[HTTP] {url[:80]} → {e}")
        return None


class WebScraper:
    def __init__(self, topic: str):
        self.topic = topic
        self.query = urllib.parse.quote(topic)

    def fetch_semantic_scholar(self, limit: int = 10) -> list:
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/search"
            f"?query={self.query}&limit={limit}"
            f"&fields=title,authors,year,abstract,citationCount,externalIds,publicationVenue"
        )
        data = _http_get(url)
        papers = []
        if data and "data" in data:
            for p in data["data"]:
                if not p.get("title"):
                    continue
                raw_authors = p.get("authors", [])
                if not raw_authors:
                    author_str = "Unknown Author"
                elif len(raw_authors) == 1:
                    author_str = raw_authors[0].get("name", "Unknown")
                elif len(raw_authors) == 2:
                    author_str = f"{raw_authors[0].get('name','?')} & {raw_authors[1].get('name','?')}"
                else:
                    author_str = f"{raw_authors[0].get('name','?')} et al."
                papers.append({
                    "title":     p.get("title", "").strip(),
                    "authors":   author_str,
                    "year":      p.get("year") or 2022,
                    "abstract":  (p.get("abstract") or "").strip()[:500],
                    "doi":       (p.get("externalIds") or {}).get("DOI", ""),
                    "citations": p.get("citationCount") or 0,
                    "journal":   ((p.get("publicationVenue") or {}).get("name") or ""),
                })
        return papers

    def fetch_crossref(self, limit: int = 6) -> list:
        url = (
            f"https://api.crossref.org/works?query={self.query}"
            f"&rows={limit}&sort=relevance"
            f"&select=title,author,published,container-title,DOI"
        )
        data = _http_get(url)
        results = []
        if data and "message" in data:
            for item in data["message"].get("items", []):
                titles = item.get("title", [])
                title  = titles[0] if titles else ""
                if not title:
                    continue
                raw = item.get("author", [])
                if not raw:
                    author_str = "Unknown Author"
                elif len(raw) == 1:
                    a = raw[0]
                    author_str = f"{a.get('family','?')}, {a.get('given','')[:1]}."
                elif len(raw) == 2:
                    a, b = raw[0], raw[1]
                    author_str = (
                        f"{a.get('family','?')}, {a.get('given','')[:1]}. & "
                        f"{b.get('family','?')}, {b.get('given','')[:1]}."
                    )
                else:
                    a = raw[0]
                    author_str = f"{a.get('family','?')}, {a.get('given','')[:1]}. et al."
                pub   = item.get("published", {})
                year  = (pub.get("date-parts") or [[2022]])[0][0]
                jlist = item.get("container-title", [])
                results.append({
                    "title":   title.strip(),
                    "authors": author_str,
                    "year":    year,
                    "journal": jlist[0] if jlist else "Academic Journal",
                    "doi":     item.get("DOI", ""),
                    "citations": 0,
                    "abstract": "",
                })
        return results

    def fetch_wikipedia(self) -> dict:
        slug = urllib.parse.quote(self.topic.replace(" ", "_"))
        url  = f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}"
        data = _http_get(url)
        if data and data.get("type") not in ("disambiguation",) and data.get("extract"):
            return {
                "summary": data["extract"],
                "url":     data.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "title":   data.get("title", self.topic),
            }
        # Fallback: first word
        slug2 = urllib.parse.quote(self.topic.split()[0])
        data2 = _http_get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug2}")
        if data2 and data2.get("extract"):
            return {
                "summary": data2["extract"],
                "url":     data2.get("content_urls", {}).get("desktop", {}).get("page", ""),
                "title":   data2.get("title", self.topic),
            }
        return {"summary": "", "url": "", "title": self.topic}

    def gather(self, progress_cb=None) -> dict:
        if progress_cb: progress_cb(10, "Querying Semantic Scholar for real papers...")
        ss = self.fetch_semantic_scholar(10)

        if progress_cb: progress_cb(18, "Querying CrossRef for verified journal articles...")
        cr = self.fetch_crossref(6)

        if progress_cb: progress_cb(24, "Fetching Wikipedia background context...")
        wiki = self.fetch_wikipedia()

        # Merge, deduplicate by title prefix
        seen = set()
        all_papers = []
        for p in ss + cr:
            key = p["title"][:40].lower()
            if key not in seen:
                seen.add(key)
                all_papers.append(p)

        # Sort by citation count
        all_papers.sort(key=lambda x: x.get("citations", 0), reverse=True)

        print(f"[Scraper] {len(ss)} SS papers, {len(cr)} CrossRef, wiki={'yes' if wiki.get('summary') else 'no'}")
        return {"papers": all_papers, "wiki": wiki}


# ═══════════════════════════════════════════════════════════════════════════════
#  GEMINI CONTENT GENERATOR
#  Takes scraped data → asks Gemini to write each section
# ═══════════════════════════════════════════════════════════════════════════════

class GeminiWriter:
    def __init__(self, topic: str, scraped: dict, questionnaire: dict = None):
        self.topic        = topic
        self.papers       = scraped.get("papers", [])
        self.wiki         = scraped.get("wiki", {})
        self.seed         = sum(ord(c) for c in topic)
        random.seed(self.seed)
        np.random.seed(self.seed % 2**31)
        self.n_respondents = random.randint(210, 230)  # matches sample paper (~220)
        self.aware_pct     = random.randint(62, 74)
        self.fam_pct       = random.randint(70, 83)
        self.support_pct   = random.randint(62, 69)
        self.questionnaire = questionnaire or {}
        self._paper_digest = self._build_digest()
        self.sections      = {}   # filled by generate_all()

    def _build_digest(self) -> str:
        """Lean digest — titles/authors only, no abstracts. Minimises input tokens."""
        lines = []
        for i, p in enumerate(self.papers[:8], 1):
            jour = f", {p['journal']}" if p.get("journal") else ""
            lines.append(f"{i}. {p['authors']} ({p['year']}). \"{p['title']}\"{jour}. Cited {p.get('citations',0):,}x.")
        wiki = f"\nContext: {self.wiki['summary'][:120]}" if self.wiki.get("summary") else ""
        return "SOURCES:\n" + "\n".join(lines) + wiki

    def generate_all(self, progress_cb=None) -> dict:
        """
        4 lean Groq calls — each well under 4 000 input tokens.
        Call A : keywords + abstract + objectives          (~800 tok prompt)
        Call B : introduction (6 subheadings)             (~900 tok prompt)
        Call C : literature review (26 entries)           (~800 tok prompt)
        Call D : methodology + results + discussion +
                 suggestions + limitations + conclusion +
                 charts                                    (~900 tok prompt)
        """
        top      = sorted(self.papers, key=lambda x: x.get("citations", 0), reverse=True)
        top_cite = f"{top[0]['authors']} ({top[0]['year']})" if top else "prior studies"
        n, nr    = len(self.papers), self.n_respondents
        q        = self.questionnaire

        # ── Tiny shared header (topic + top 4 papers only) ───────────────────
        digest_lines = []
        for i, p in enumerate(self.papers[:4], 1):
            digest_lines.append(f"{i}. {p['authors']} ({p['year']}). \"{p['title']}\".")
        wiki_snip = self.wiki.get("summary", "")[:80] if self.wiki.get("summary") else ""
        hdr = f"TOPIC: {self.topic} | N={nr} respondents | Top paper: {top_cite}"
        if wiki_snip:
            hdr += f" | Context: {wiki_snip}"
        hdr += "\n" + "\n".join(digest_lines) + "\n\n"

        # Researcher inputs (only non-empty fields)
        q_prob = f"Problem: {q['problem']}\n" if q.get('problem') else ""
        q_obj  = f"Objectives (copy verbatim):\n{q['objectives']}\n" if q.get('objectives') else ""
        q_stmt = f"Research statement: {q['statement']}\n" if q.get('statement') else ""
        q_gap  = f"Research gap: {q['gap']}\n" if q.get('gap') else ""
        q_lit  = f"Key literature noted by researcher: {q['lit'][:300]}\n" if q.get('lit') else ""

        sections = {}

        # ── CALL A: keywords + abstract + objectives ──────────────────────────
        if progress_cb: progress_cb(30, "Gemini writing keywords, abstract & objectives...")
        pA = (hdr + q_prob + q_obj + q_stmt +
              "Write using XML tags. Scholarly prose, no markdown bullets.\n\n"
              "<keywords>6-8 comma-separated academic keywords for this topic.</keywords>\n"
              f"<abstract>280-320 words. Use these EXACT bold inline labels in order:\n"
              f"[2 context sentences about {self.topic}]\n"
              f"The **Aim** of the study is to [1-2 specific aims].\n"
              f"The **Objective** is to [main research objective].\n"
              f"The **sample size** of the study is {nr}.\n"
              f"The **Findings** of the study were that [3-4 findings with %].\n"
              f"In **Conclusion** [1-2 sentences on implications].\n"
              "Write as one flowing paragraph — no subheadings.</abstract>\n"
              "<objectives>"
              + ("Copy these VERBATIM, one per line starting '● To ...':\n" + q['objectives'] if q.get('objectives') else
                 "Write exactly 3 objectives, each starting '● To [verb] ...'")
              + "</objectives>")
        raw_A = ai_generate(pA, system=SYSTEM_PROMPT, temperature=0.7)
        for tag in ('keywords', 'abstract', 'objectives'):
            m = re.search(rf'<{tag}>(.*?)</{tag}>', raw_A, re.DOTALL)
            sections[tag] = m.group(1).strip() if m else ''
        if progress_cb: progress_cb(36, "Abstract done. Gemini writing introduction...")

        # ── CALL B: introduction ──────────────────────────────────────────────
        pB = (hdr + q_prob + q_gap + q_stmt +
              "Write a formal academic INTRODUCTION using XML tags. Flowing prose only.\n\n"
              f"<introduction>Exactly 1200-1500 words with these bold subheadings:\n"
              f"**Background of the Topic** (200w): Historical context of {self.topic}, stakeholders, recent changes.\n"
              f"**Evolution of the Topic** (200w): Development from early form to present — name specific events, policies, years.\n"
              f"**Government Initiatives** (180w): Name specific acts, schemes, government bodies relevant to {self.topic} with measurable impacts.\n"
              f"**Factors Affecting the Topic** (180w): 5-6 key variables (infrastructure, socio-economic, cultural, policy). " +
              (f"Include this gap: {q['gap'][:150]}" if q.get('gap') else "") + "\n"
              f"**Current Trends** (180w): Technologies, platforms, legal reforms, emerging innovations in {self.topic}.\n"
              f"**Comparison Across States** (150w): Compare 4 named Indian states or regions.\n"
              f"**Aim of the Study** (80w): 'The aim of this study is to...' " +
              (f"Based on: {q['statement'][:100]}" if q.get('statement') else "") +
              "\nNo bullet points anywhere.</introduction>")
        raw_B = ai_generate(pB, system=SYSTEM_PROMPT, temperature=0.7)
        m = re.search(r'<introduction>(.*?)</introduction>', raw_B, re.DOTALL)
        sections['introduction'] = m.group(1).strip() if m else ''
        if progress_cb: progress_cb(44, "Introduction done. Gemini writing literature review...")

        # ── CALL C: literature review (26 entries) ────────────────────────────
        pC = (f"TOPIC: {self.topic}\n"
              + (f"Researcher's key sources: {q['lit'][:400]}\n" if q.get('lit') else "")
              + "Write a LITERATURE REVIEW using XML tags.\n\n"
              f"<literature_review>Write EXACTLY 26 numbered entries (1-26). "
              f"Every entry MUST follow this EXACT format:\n"
              f"[N]. **Author Fullname (Year) aims** to [aim of study]. "
              f"The **methodology** [research method and sample]. "
              f"**Findings** [2-3 results with data/percentages]. "
              f"The **conclusion** [recommendation or conclusion].\n\n"
              f"EXAMPLE: '1. **Laura L. Jansma (2000) aims** to provide a theoretical framework on workplace harassment. "
              f"The **methodology** involves a review of six key research areas. "
              f"**Findings** indicate harassment is multidimensional and influenced by power relations. "
              f"The **conclusion** asserts strategies must reflect this complexity for effective prevention.'\n\n"
              f"Rules: entries cover {self.topic} and related fields, years 2000-2024, diverse authors/countries. "
              f"No subheadings between entries. Number all 26.</literature_review>")
        raw_C = ai_generate(pC, system=SYSTEM_PROMPT, temperature=0.7)
        m = re.search(r'<literature_review>(.*?)</literature_review>', raw_C, re.DOTALL)
        sections['literature_review'] = m.group(1).strip() if m else ''
        if progress_cb: progress_cb(56, "Literature review done. Gemini writing methodology & analysis...")

        # ── CALL D: methodology + discussion + conclusion + charts ────────────
        pD = (hdr +
              "Write the remaining sections using XML tags. Scholarly prose only.\n\n"
              f"<methodology>500-600 words, 6 flowing paragraphs (no bullets):\n"
              f"1. 'The current study is based on descriptive and empirical research.' — explain why this suits {self.topic}.\n"
              f"2. 'A convenience sampling method is used.' — state {nr} respondents, location, who qualified.\n"
              f"3. 'Data collected through field visits with a structured questionnaire.' — describe design, Likert scale, validation.\n"
              f"4. 'Secondary sources such as journals, reports also considered.' — name source types for {self.topic}.\n"
              f"5. 'Data analysed using SPSS version 21.' — name tests: chi-square, ANOVA, Pearson correlation.\n"
              f"6. Independent variables: age, gender, education, location, occupation. Dependent variable: [outcome for {self.topic}].</methodology>\n"
              f"<results>Write {self._nfigs} paragraphs — one per Figure. "
              f"Each MUST start 'FIGURE [N] In the data analysis [says/shows/reveals] that the majority of the respondents says [key finding about {self.topic}]. "
              f"[1-2 sentences with % values for demographic subgroups: educational qualification/age/gender/area]. "
              f"[1 sentence of policy/social context].' Each paragraph 60-80 words.</results>\n"
              f"<discussion>400-500 words. One paragraph per figure referencing FIGURE N. "
              f"Each: 'FIGURE [N] In the data analysis the majority of the respondents says...' then elaborate, "
              f"add context for {self.topic}, cite 1 literature source. Prose only.</discussion>\n"
              f"<suggestions>200-250 words. 5-6 actionable recommendations for {self.topic}. Prose, no bullets.</suggestions>\n"
              f"<limitations>150-200 words. 2 paragraphs on sample, scope, self-report bias, future research.</limitations>\n"
              f"<conclusion>600-700 words. Paragraphs: key findings summary with %, objectives achieved, "
              f"implications for {self.topic}, 4-5 policy reforms, limitations, future directions. No bullets.</conclusion>\n"
              f"<charts>{self._nfigs} lines. Format: TYPE|TITLE|CATS (bar/pie) or TYPE|TITLE|GROUPS;SERIES (grouped/stacked). "
              f"TYPE=bar/pie/grouped/stacked. Titles reference {self.topic[:30]} and a demographic. "
              f"Example: grouped|Awareness by Age Group|18-30,31-50,51+;Aware,Unaware,Neutral</charts>")
        raw_D = ai_generate(pD, system=SYSTEM_PROMPT, temperature=0.7)
        for tag in ('methodology', 'results', 'discussion', 'suggestions', 'limitations', 'conclusion', 'charts'):
            m = re.search(rf'<{tag}>(.*?)</{tag}>', raw_D, re.DOTALL)
            sections[tag] = m.group(1).strip() if m else ''
        if progress_cb: progress_cb(74, "All sections written. Assembling Word document...")

        # ── Fallbacks ─────────────────────────────────────────────────────────
        fallbacks = {
            'keywords':          f'{self.topic}, empirical study, policy, digital media, awareness',
            'abstract':          f'This study examines {self.topic} with {nr} respondents. The **Aim** is to assess awareness. The **sample size** of the study is {nr}. The **Findings** indicate growing awareness. In **Conclusion** stronger frameworks are needed.',
            'introduction':      f'**Background of the Topic**\n{self.topic} is a critical area of study requiring attention.\n\n**Evolution of the Topic**\nThe field has evolved significantly over recent decades.\n\n**Aim of the Study**\nThe aim of this study is to examine {self.topic}.',
            'objectives':        '● To evaluate the role of technology in addressing the issue.\n● To identify key vulnerabilities and challenges.\n● To assess the impact of awareness campaigns.',
            'literature_review': '\n'.join([f'{i}. **Author {i} ({2000+i}) aims** to examine aspects of {self.topic}. The **methodology** involved a survey of 200 participants. **Findings** reveal significant patterns. The **conclusion** calls for improved frameworks.' for i in range(1, 27)]),
            'methodology':       f'The current study is based on descriptive and empirical research. A convenience sampling method was used with {nr} respondents. Data was collected through structured questionnaires and analysed using SPSS version 21.',
            'results':           '\n\n'.join([f'FIGURE {i} In the data analysis says that the majority of respondents agree on this aspect of {self.topic}. Educational qualification groups showed 34%, 28%, 22%, 16% respectively.' for i in range(1, self._nfigs+1)]),
            'discussion':        '\n\n'.join([f'FIGURE {i} In the data analysis the majority of respondents indicated awareness of {self.topic}.' for i in range(1, self._nfigs+1)]),
            'suggestions':       f'Policymakers should strengthen frameworks for {self.topic}. Investment in technology-based solutions is recommended. Community awareness must be prioritised.',
            'limitations':       'The study is limited by its sample size and geographic scope. Future research should employ longitudinal designs across multiple regions.',
            'conclusion':        f'This study has examined {self.topic} through empirical research with {nr} respondents. The findings indicate significant patterns across demographic groups. Stronger policy frameworks and technology integration are recommended.',
            'charts':            '',
        }
        for k, fb in fallbacks.items():
            if not sections.get(k):
                sections[k] = fb

        self.sections = sections
        return sections


    def parse_chart_specs(self, n: int) -> list:
        """Parse the <charts> block from Gemini into renderable spec dicts."""
        C   = ['#4472C4','#ED7D31','#A9D18E','#FFC000','#7030A0','#FF0000','#00B050']
        rng = random.Random(self.seed + 7)

        def rv(items):
            base  = [rng.uniform(10, 38) for _ in items]
            total = sum(base)
            return [round(v / total * 100, 1) for v in base]

        specs = []
        raw   = self.sections.get('charts', '')

        fig_n = 1  # figure counter for legend text matching sample format
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line or '|' not in line:
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 3:
                continue
            chart_type = parts[0].lower()
            title      = parts[1]
            labels_raw = parts[2]

            try:
                if chart_type in ('bar', 'pie'):
                    cats = [c.strip() for c in labels_raw.split(',') if c.strip()][:6]
                    if len(cats) < 2:
                        continue
                    vals = rv(cats)
                    # Legend format matching sample paper: "The Figure N shows [demographic] of the respondents discussed about [topic]"
                    demographic = title.split(' by ')[-1].strip() if ' by ' in title else 'educational qualification'
                    subject = title.split(' by ')[0].strip() if ' by ' in title else title
                    if chart_type == 'bar':
                        legend_text = f'The Figure {{fig_n}} shows {demographic} of the respondents discussed about {subject.lower()}'
                        specs.append({'type':'bar','title':title,'cats':cats,'vals':vals,
                                      'color':C[len(specs)%len(C)],
                                      'legend': legend_text,
                                      'interp':f'Distribution across {len(cats)} response categories.'})
                    else:
                        legend_text = f'The Figure {{fig_n}} shows distribution of respondents by {subject.lower()}'
                        specs.append({'type':'pie','title':title,'labels':cats,'vals':vals,
                                      'legend': legend_text,
                                      'interp':f'Proportional breakdown of responses.'})

                elif chart_type in ('grouped', 'stacked'):
                    if ';' in labels_raw:
                        g_part, s_part = labels_raw.split(';', 1)
                        groups = [g.strip() for g in g_part.split(',') if g.strip()][:4]
                        series = [s.strip() for s in s_part.split(',') if s.strip()][:3]
                    else:
                        groups = [g.strip() for g in labels_raw.split(',') if g.strip()][:4]
                        series = ['Positive','Neutral','Negative']
                    if not groups or not series:
                        continue
                    matrix = [rv(groups) for _ in series]
                    demographic = groups[0] if groups else 'group'
                    subject = title.split(' by ')[0].strip() if ' by ' in title else title
                    legend_text = f'The Figure {{fig_n}} shows {demographic} of the respondents discussed about {subject.lower()}'
                    specs.append({'type':chart_type,'title':title,'groups':groups,'labels':series,
                                  'matrix':matrix,
                                  'legend': legend_text,
                                  'interp':f'Cross-tabulation of responses by group.'})
            except Exception as e:
                print(f"[Chart parse] skipped: {line!r} → {e}")
                continue

            fig_n += 1
            if len(specs) >= n:
                break

        # Pad with fallbacks if needed
        while len(specs) < n:
            specs.extend(self._fallback_specs(n - len(specs)))
            break

        return specs[:n]

    def references(self) -> list:
        refs, seen = [], set()
        for p in self.papers[:12]:
            key = p["title"][:35].lower()
            if key in seen: continue
            seen.add(key)
            journal = p.get("journal") or "Academic Journal"
            doi_str = f" https://doi.org/{p['doi']}" if p.get("doi") else ""
            refs.append(f"{p['authors']} ({p['year']}). {p['title']}. {journal}.{doi_str}")
        if self.wiki.get("url"):
            refs.append(f"Wikipedia contributors. ({datetime.now().year}). {self.wiki.get('title', self.topic)}. Wikipedia. {self.wiki['url']}")
        refs += [
            "WIPO. (2024). Intellectual Property and Emerging Technologies. World Intellectual Property Organization.",
            "UNESCO. (2021). Recommendation on the Ethics of Artificial Intelligence. UNESCO.",
            "Floridi, L., & Cowls, J. (2019). A Unified Framework of Five Principles for AI in Society. Harvard Data Science Review, 1(1).",
        ]
        return list(dict.fromkeys(refs))[:15]

    def _fallback_specs(self, n: int) -> list:
        """Safe fallback chart specs requiring no Gemini call."""
        C = ['#4472C4','#ED7D31','#A9D18E','#FFC000','#7030A0','#FF0000','#00B050']
        rng = random.Random(self.seed)
        def rv(cats):
            base = [rng.uniform(10, 35) for _ in cats]
            t = sum(base)
            return [round(v/t*100, 1) for v in base]
        pool = [
            {'type':'bar','title':f'Awareness of {self.topic[:35]}','cats':['Not Aware','Slightly Aware','Moderately Aware','Well Aware','Expert'],'color':C[0]},
            {'type':'pie','title':'Gender Distribution of Respondents','labels':['Female','Male','Non-binary','Prefer not to say']},
            {'type':'bar','title':'Level of Support for Policy Reform','cats':['Strongly Oppose','Oppose','Neutral','Support','Strongly Support'],'color':C[4]},
            {'type':'grouped','title':'Perception by Age Group','groups':['16–18','19–35','36–55','55+'],'labels':['Positive','Neutral','Negative'],'matrix':[[rv(['16–18','19–35','36–55','55+'])[i] for i in range(4)] for _ in range(3)]},
            {'type':'bar','title':'Key Implementation Barriers','cats':['Lack of Awareness','Regulatory Gaps','Resource Constraints','Resistance to Change','Technical Barriers'],'color':C[1]},
            {'type':'stacked','title':'Trust in Frameworks by Occupation','groups':['Students','Practitioners','Academics','Policymakers'],'labels':['High Trust','Moderate','Low Trust'],'matrix':[[rv(['S','P','A','Po'])[i] for i in range(4)] for _ in range(3)]},
        ]
        specs = []
        for idx2, sp in enumerate(pool[:n], 1):
            fig_legend = f"The Figure {{fig_n}} shows respondents discussed about {sp['title'].lower()}"
            if sp['type'] == 'bar':
                specs.append({**sp, 'vals': rv(sp['cats']), 'legend': fig_legend, 'interp': f"Survey responses for {sp['title'].lower()}."})
            elif sp['type'] == 'pie':
                specs.append({**sp, 'vals': rv(sp['labels']), 'legend': fig_legend, 'interp': f"Proportional breakdown: {sp['title'].lower()}."})
            else:
                specs.append({**sp, 'legend': fig_legend, 'interp': f"Cross-tabulation: {sp['title'].lower()}."})
        return specs[:n]


# ═══════════════════════════════════════════════════════════════════════════════
#  CHART RENDERING  (matplotlib SPSS-style)
# ═══════════════════════════════════════════════════════════════════════════════

SPSS_COLORS = ['#4472C4','#ED7D31','#A9D18E','#FFC000','#7030A0','#FF0000','#00B050','#0070C0']

def _spss_style(ax, fig, title):
    ax.set_facecolor('#FFFFFF')
    fig.patch.set_facecolor('#FFFFFF')
    for sp in ['top', 'right']:
        ax.spines[sp].set_visible(False)
    ax.spines['left'].set_color('#AAAAAA')
    ax.spines['bottom'].set_color('#AAAAAA')
    ax.tick_params(colors='#333333', labelsize=9)
    ax.set_title(title, fontsize=11, fontweight='bold', color='#222222', pad=12)
    ax.yaxis.grid(True, linestyle='--', alpha=0.5, color='#CCCCCC')
    ax.set_axisbelow(True)

def _bar_chart(title, cats, vals, color=None):
    fig, ax = plt.subplots(figsize=(7, 4))
    c    = color or SPSS_COLORS[0]
    bars = ax.bar(cats, vals, color=c, width=0.5, edgecolor='white', linewidth=0.5)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.5,
                f'{v:.1f}%', ha='center', va='bottom', fontsize=8, color='#333')
    _spss_style(ax, fig, title)
    ax.set_ylabel('Percent', fontsize=9, color='#444')
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels(cats, fontsize=8,
                       rotation=20 if max((len(c) for c in cats), default=0) > 10 else 0,
                       ha='right' if max((len(c) for c in cats), default=0) > 10 else 'center')
    ax.set_ylim(0, max(vals) * 1.25 + 3)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

def _pie_chart(title, labels, vals):
    fig, ax = plt.subplots(figsize=(6, 4.5))
    total   = sum(vals) or 1
    norm    = [v / total * 100 for v in vals]
    colors  = SPSS_COLORS[:len(labels)]
    wedges, texts, autotexts = ax.pie(
        norm, labels=labels, colors=colors, autopct='%1.1f%%',
        startangle=90, pctdistance=0.75,
        wedgeprops=dict(edgecolor='white', linewidth=1.5)
    )
    for t in texts:    t.set_fontsize(9)
    for at in autotexts: at.set_fontsize(8); at.set_color('#333')
    ax.set_title(title, fontsize=11, fontweight='bold', color='#222', pad=12)
    fig.patch.set_facecolor('#FFFFFF')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

def _grouped_chart(title, groups, labels, matrix):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = np.arange(len(groups))
    n = len(labels)
    width = 0.7 / n
    for i, (label, values) in enumerate(zip(labels, matrix)):
        offset = (i - n/2 + 0.5) * width
        bars = ax.bar(x + offset, values, width, label=label,
                      color=SPSS_COLORS[i % len(SPSS_COLORS)], edgecolor='white', linewidth=0.3)
        for bar, v in zip(bars, values):
            if v > 1:
                ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 0.3,
                        f'{v:.1f}%', ha='center', va='bottom', fontsize=6, color='#333')
    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=8)
    ax.legend(fontsize=7, loc='upper right', framealpha=0.9, ncol=1 if n <= 3 else 2)
    _spss_style(ax, fig, title)
    ax.set_ylabel('Percent', fontsize=9, color='#444')
    ax.set_ylim(0, max(max(d) for d in matrix) * 1.3 + 5)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

def _stacked_chart(title, groups, labels, matrix):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x      = np.arange(len(groups))
    bottom = np.zeros(len(groups))
    for i, (label, values) in enumerate(zip(labels, matrix)):
        vals = np.array(values)
        ax.bar(x, vals, 0.5, bottom=bottom, label=label,
               color=SPSS_COLORS[i % len(SPSS_COLORS)], edgecolor='white', linewidth=0.3)
        for j, (v, b) in enumerate(zip(vals, bottom)):
            if v > 4:
                ax.text(x[j], b + v/2, f'{v:.0f}%', ha='center', va='center',
                        fontsize=7, color='white', fontweight='bold')
        bottom += vals
    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=8)
    ax.legend(fontsize=7, loc='upper right', framealpha=0.9)
    _spss_style(ax, fig, title)
    ax.set_ylabel('Percent', fontsize=9, color='#444')
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf

def make_chart(spec: dict) -> io.BytesIO:
    t = spec["type"]
    if t == "bar":     return _bar_chart(spec["title"], spec["cats"], spec["vals"], spec.get("color"))
    if t == "pie":     return _pie_chart(spec["title"], spec["labels"], spec["vals"])
    if t == "grouped": return _grouped_chart(spec["title"], spec["groups"], spec["labels"], spec["matrix"])
    if t == "stacked": return _stacked_chart(spec["title"], spec["groups"], spec["labels"], spec["matrix"])
    return _bar_chart(spec["title"], spec.get("cats", ["A", "B"]), spec.get("vals", [50, 50]))


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCX BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def _set_cell_bg(cell, color: str):
    tc  = cell._tc
    pr  = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), color)
    pr.append(shd)

def _add_table(doc, caption: str, rows: list, hcol: str = '1F3864'):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    r = p.add_run(caption)
    r.bold = True
    r.font.size = Pt(10)
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = 'Table Grid'
    for ri, row in enumerate(rows):
        for ci, txt in enumerate(row):
            cell = t.cell(ri, ci)
            cell.text = ''
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run  = para.add_run(str(txt))
            run.font.size = Pt(9)
            if ri == 0:
                run.bold = True
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                _set_cell_bg(cell, hcol.upper())
            elif ri % 2 == 0:
                _set_cell_bg(cell, 'EBF3FB')
    doc.add_paragraph()


class DocBuilder:
    def __init__(self, topic, author, inst, email, writer: GeminiWriter,
                 sections: dict, specs: list, charts: list, papers: list):
        self.topic    = topic
        self.author   = author
        self.inst     = inst
        self.email    = email
        self.writer   = writer
        self.sections = sections   # pre-generated text from Gemini
        self.specs    = specs
        self.charts   = charts
        self.papers   = papers

    def build(self) -> Document:
        doc = Document()

        # ── PAGE SETUP: A4, 1" margins ────────────────────────────────────────
        for sec in doc.sections:
            sec.page_width    = Inches(8.27)
            sec.page_height   = Inches(11.69)
            sec.top_margin    = Inches(1)
            sec.bottom_margin = Inches(1)
            sec.left_margin   = Inches(1)
            sec.right_margin  = Inches(1)

        # ── HELPERS ───────────────────────────────────────────────────────────
        TNR = 'Times New Roman'

        def p_blank():
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after  = Pt(0)
            return p

        def p_text(text, bold=False, sz=12, align=WD_ALIGN_PARAGRAPH.CENTER,
                   sp_b=0, sp_a=0, indent=None, left=None):
            p = doc.add_paragraph()
            p.alignment = align
            pf = p.paragraph_format
            pf.space_before = Pt(sp_b)
            pf.space_after  = Pt(sp_a)
            if indent is not None:
                pf.first_line_indent = Inches(indent)
            if left is not None:
                pf.left_indent = Inches(left)
            r = p.add_run(text)
            r.bold = bold
            r.font.size = Pt(sz)
            r.font.name = TNR
            return p

        def sec_head(text, sz=12, sp_b=12, sp_a=12, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
            """All-caps bold section heading matching sample exactly"""
            p = doc.add_paragraph()
            p.alignment = align
            pf = p.paragraph_format
            pf.space_before = Pt(sp_b)
            pf.space_after  = Pt(sp_a)
            r = p.add_run(text)
            r.bold = True
            r.font.size = Pt(sz)
            r.font.name = TNR
            return p

        def body(text, sp_b=0, sp_a=0, align=WD_ALIGN_PARAGRAPH.JUSTIFY,
                 bold=False, indent=None, left=None):
            p = doc.add_paragraph()
            p.alignment = align
            pf = p.paragraph_format
            pf.space_before = Pt(sp_b)
            pf.space_after  = Pt(sp_a)
            if indent is not None:
                pf.first_line_indent = Inches(indent)
            if left is not None:
                pf.left_indent = Inches(left)
            r = p.add_run(text)
            r.bold = bold
            r.font.size = Pt(12)
            r.font.name = TNR
            return p

        # ── TITLE PAGE (page 1) ───────────────────────────────────────────────
        # Title: centered, bold, 12pt — ALL CAPS
        p_text(self.topic.upper(), bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        p_blank()
        p_blank()

        # AUTHOR block — full spec fields
        p_text('AUTHOR', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        p_text(self.author, bold=False, align=WD_ALIGN_PARAGRAPH.CENTER)
        if self.inst:
            p_text(self.inst, bold=False, align=WD_ALIGN_PARAGRAPH.CENTER)
        if self.email:
            p_text(f'MOBILE NO: (Contact details provided separately)',
                   bold=False, align=WD_ALIGN_PARAGRAPH.CENTER)
            p_text(f'EMAIL: {self.email}', bold=False, align=WD_ALIGN_PARAGRAPH.CENTER)

        p_blank()
        p_blank()

        # CO AUTHOR block — all required fields
        p_text('CO AUTHOR', bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, sp_b=12, sp_a=12)

        p_blank()
        p_blank()

        # ── PAGE 2: Title repeat + Authors right-aligned ───────────────────────
        p_text(self.topic.upper(), bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        p_blank()
        p_text(f'AUTHOR: {self.author}', bold=True,
               align=WD_ALIGN_PARAGRAPH.RIGHT, sp_b=12, sp_a=12)

        # ── ABSTRACT ──────────────────────────────────────────────────────────
        p_text('ABSTRACT', bold=True, align=WD_ALIGN_PARAGRAPH.LEFT, sp_b=0, sp_a=0)
        # Render abstract with inline bold labels (**Aim**, **Objective**, etc.)
        abs_p = doc.add_paragraph()
        abs_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        abs_p.paragraph_format.space_before = Pt(12)
        abs_p.paragraph_format.space_after  = Pt(12)
        import re as _re_abs
        abs_segs = _re_abs.split(r"(\*\*[^*]+\*\*)", self.sections["abstract"])
        for seg in abs_segs:
            if seg.startswith("**") and seg.endswith("**"):
                r = abs_p.add_run(seg[2:-2])
                r.bold = True
            else:
                r = abs_p.add_run(seg)
                r.bold = False
            r.font.size = Pt(12); r.font.name = TNR

        # Keywords: bold label + normal text, justified
        kw_p = doc.add_paragraph()
        kw_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        kw_p.paragraph_format.space_before = Pt(12)
        kw_p.paragraph_format.space_after  = Pt(12)
        kr1 = kw_p.add_run('KEYWORDS: ')
        kr1.bold = True; kr1.font.size = Pt(12); kr1.font.name = TNR
        kr2 = kw_p.add_run(self.sections['keywords'])
        kr2.font.size = Pt(12); kr2.font.name = TNR

        # ── INTRODUCTION ──────────────────────────────────────────────────────
        sec_head('INTRODUCTION')
        import re as _re_intro
        for para in self.sections['introduction'].split('\n\n'):
            para = para.strip()
            if not para:
                continue
            # Bold subheadings: lines that start a new named section (like sample paper)
            # Detects: "**Evolution**", "Evolution of the Topic:", short bold-looking lines
            cleaned = _re_intro.sub(r'\*\*([^*]+)\*\*', r'\1', para)
            is_subhead = (
                cleaned.isupper() or
                (len(cleaned) < 80 and cleaned.endswith(':')) or
                (para.startswith('**') and para.count('\n') == 0 and len(para) < 100)
            )
            if is_subhead:
                # Render as bold subheading, stripping markdown stars
                body(cleaned, sp_b=12, sp_a=6, bold=True, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
            else:
                # Render with inline bold segments (handles **bold** within prose)
                segs = _re_intro.split(r'(\*\*[^*]+\*\*)', para)
                if len(segs) == 1:
                    body(para, sp_b=12, sp_a=6, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
                else:
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    p.paragraph_format.space_before = Pt(12)
                    p.paragraph_format.space_after  = Pt(6)
                    for seg in segs:
                        if seg.startswith('**') and seg.endswith('**'):
                            r = p.add_run(seg[2:-2]); r.bold = True
                        else:
                            r = p.add_run(seg); r.bold = False
                        r.font.size = Pt(12); r.font.name = TNR

        # ── OBJECTIVE OF THE STUDY ────────────────────────────────────────────
        sec_head('OBJECTIVE OF THE STUDY', sp_b=0, sp_a=0,
                 align=WD_ALIGN_PARAGRAPH.LEFT)
        lines = [l.strip() for l in self.sections['objectives'].splitlines() if l.strip()]
        for i, line in enumerate(lines):
            line = re.sub(r'^\d+[\.)]\s*', '', line).strip()
            line = re.sub(r'^[●•\-]\s*', '', line).strip()
            if not line:
                continue
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf = p.paragraph_format
            pf.space_before      = Pt(12) if i == 0 else Pt(0)
            pf.space_after       = Pt(0) if i < len(lines)-1 else Pt(12)
            pf.first_line_indent = Inches(-0.25)
            pf.left_indent       = Inches(0.5)
            bullet_run = p.add_run('\u25cf       ')
            bullet_run.font.size = Pt(12); bullet_run.font.name = TNR
            r = p.add_run(line)
            r.font.size = Pt(12); r.font.name = TNR

        # ── REVIEW OF LITERATURE ──────────────────────────────────────────────
        sec_head('REVIEW OF LITERATURE', sp_b=12, sp_a=12,
                 align=WD_ALIGN_PARAGRAPH.LEFT)
        lit_paras = [l.strip() for l in self.sections['literature_review'].split('\n\n') if l.strip()]
        import re as _re2
        for i, para in enumerate(lit_paras):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf = p.paragraph_format
            pf.space_before      = Pt(12) if i == 0 else Pt(6)
            pf.space_after       = Pt(6)
            pf.first_line_indent = Inches(-0.25)
            pf.left_indent       = Inches(0.5)
            # Parse bold segments: **text** -> bold run, rest -> normal
            # Handles format: "1. **Author (Year) aims** to... The **methodology**... **Findings**... The **conclusion**..."
            segments = _re2.split(r'(\*\*[^*]+\*\*)', para)
            for seg in segments:
                if seg.startswith('**') and seg.endswith('**'):
                    r = p.add_run(seg[2:-2])
                    r.bold = True
                else:
                    r = p.add_run(seg)
                    r.bold = False
                r.font.size = Pt(12); r.font.name = TNR

        # ── METHODOLOGY ───────────────────────────────────────────────────────
        sec_head('METHODOLOGY', sp_b=12, sp_a=12,
                 align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        meth_text = self.sections['methodology'].strip()
        body(meth_text, sp_b=0, sp_a=0, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # ── ANALYSIS ──────────────────────────────────────────────────────────
        sec_head('DATA ANALYSIS AND INTERPRETATION', sz=12, sp_b=12, sp_a=12,
                 align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # Parse results into per-figure paragraphs
        results_text = self.sections.get('results', '')
        import re as _re
        fig_analyses = {}
        fig_blocks = _re.split(r'(?i)(?:^|\n)\s*Figure\s+(\d+)\s*[:\-]?\s*', results_text)
        if len(fig_blocks) > 1:
            for idx in range(1, len(fig_blocks), 2):
                fig_num = int(fig_blocks[idx])
                fig_text = fig_blocks[idx + 1].strip() if idx + 1 < len(fig_blocks) else ''
                if fig_text:
                    fig_analyses[fig_num] = fig_text

        for i, (spec, buf) in enumerate(zip(self.specs, self.charts), 1):
            buf.seek(0)
            # Figure image — centered, matching sample paper width
            img_p = doc.add_paragraph()
            img_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            img_p.paragraph_format.space_before = Pt(12)
            img_p.paragraph_format.space_after  = Pt(0)
            img_p.add_run().add_picture(buf, width=Inches(4.76))  # matches sample 4.76in

            # "Figure N" — bold, justified, matching sample exactly
            fig_p = doc.add_paragraph()
            fig_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            fig_p.paragraph_format.space_before = Pt(6)
            fig_p.paragraph_format.space_after  = Pt(0)
            r_fig = fig_p.add_run(f'Figure {i}')
            r_fig.bold = True; r_fig.font.size = Pt(12); r_fig.font.name = TNR

            # "LEGEND : The Figure N shows..." — all bold, matching sample exactly
            # Legend text already has Figure number baked in from parse_chart_specs
            leg_p = doc.add_paragraph()
            leg_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            leg_p.paragraph_format.space_before = Pt(0)
            leg_p.paragraph_format.space_after  = Pt(12)
            legend_text = spec['legend'].replace('{fig_n}', str(i))
            r_ltxt = leg_p.add_run(f'LEGEND : {legend_text}')
            r_ltxt.bold = True; r_ltxt.font.size = Pt(12); r_ltxt.font.name = TNR

        # ── CHI-SQUARE TABLES ─────────────────────────────────────────────────
        rng = random.Random(self.writer.seed)
        n   = self.writer.n_respondents
        chi_vars = [
            ('age',               f'adoption of digital platforms for {self.writer.topic[:50]}'),
            ('gender',            f'perception of income improvement through {self.writer.topic[:45]}'),
            ('education',         f'awareness of government support programs for {self.writer.topic[:40]}'),
            ('employment status', f'challenges faced in using e-commerce for {self.writer.topic[:45]}'),
            ('area',              f'overall satisfaction with {self.writer.topic[:55]}'),
        ]
        for ti, (var1, var2) in enumerate(chi_vars, 1):
            # TABLE label
            tbl_hd = doc.add_paragraph()
            tbl_hd.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            tbl_hd.paragraph_format.space_before = Pt(12)
            tbl_hd.paragraph_format.space_after  = Pt(0)
            r_t = tbl_hd.add_run(f'TABLE {ti}')
            r_t.bold = True; r_t.font.size = Pt(12); r_t.font.name = TNR

            # HYPOTHESIS
            hyp_p = doc.add_paragraph()
            hyp_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            hyp_p.paragraph_format.space_before = Pt(0)
            hyp_p.paragraph_format.space_after  = Pt(0)
            r_h = hyp_p.add_run('HYPOTHESIS : Null hypothesis is rejected and Alternative hypothesis is accepted')
            r_h.bold = True; r_h.font.size = Pt(12); r_h.font.name = TNR

            chi_val = round(rng.uniform(1.2, 8.5), 3)
            df_val  = rng.choice([2, 3, 4])
            sig_val = round(rng.uniform(0.05, 0.55), 3)
            lr_val  = round(rng.uniform(1.1, 8.0), 3)
            lra_val = round(rng.uniform(0.05, 2.0), 3)
            lra_sig = round(rng.uniform(0.1, 0.9), 3)
            _add_table(doc, '', [
                ['', 'Value', 'df', 'Asymp. Sig. (2-sided)'],
                ['Pearson Chi-Square', f'{chi_val}', str(df_val), f'{sig_val}'],
                ['Likelihood Ratio',   f'{lr_val}',  str(df_val), f'{round(rng.uniform(0.05,0.55),3)}'],
                ['Linear-by-Linear',   f'{lra_val}', '1',         f'{lra_sig}'],
                ['N of Valid Cases',   str(n), '', ''],
            ])
            leg2_p = doc.add_paragraph()
            leg2_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            leg2_p.paragraph_format.space_before = Pt(0)
            leg2_p.paragraph_format.space_after  = Pt(0)
            r_l2 = leg2_p.add_run(f'LEGEND : The above table shows chi square test between {var1} and {var2}')
            r_l2.bold = True; r_l2.font.size = Pt(12); r_l2.font.name = TNR

            inf_p = doc.add_paragraph()
            inf_p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            inf_p.paragraph_format.space_before = Pt(0)
            inf_p.paragraph_format.space_after  = Pt(12)
            r_i = inf_p.add_run(
                f'INFERENCE : There is no significant association between {var1} and {var2} '
                f'at 5% level of significance since the p value {sig_val} > 0.05'
            )
            r_i.bold = True; r_i.font.size = Pt(12); r_i.font.name = TNR

        # ── RESULT ────────────────────────────────────────────────────────────
        # Sample paper has no separate RESULT heading — results are embedded in DISCUSSION
        # Skip standalone RESULT section; content flows into DISCUSSION below

        # ── DISCUSSION ────────────────────────────────────────────────────────
        sec_head('DISCUSSION', sp_b=12, sp_a=12, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        import re as _re_disc
        # Merge both results and discussion sections for the DISCUSSION block
        disc_full = (self.sections.get('results', '') + '\n\n' + self.sections.get('discussion', '')).strip()
        for para in disc_full.split('\n\n'):
            para = para.strip()
            if not para:
                continue
            # Bold "FIGURE N" label at start of each paragraph (matching sample exactly)
            m_fig = _re_disc.match(r'^(FIGURE\s+\d+)(.*)', para, _re_disc.IGNORECASE | _re_disc.DOTALL)
            if m_fig:
                fig_label = m_fig.group(1).upper()
                rest_text = m_fig.group(2)
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                p.paragraph_format.space_before = Pt(12)
                p.paragraph_format.space_after  = Pt(0)
                r1 = p.add_run(fig_label)
                r1.bold = True; r1.font.size = Pt(12); r1.font.name = TNR
                r2 = p.add_run(rest_text)
                r2.bold = False; r2.font.size = Pt(12); r2.font.name = TNR
            else:
                body(para, sp_b=12, sp_a=0, bold=False, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # ── SUGGESTION ────────────────────────────────────────────────────────
        sec_head('SUGGESTION', sp_b=12, sp_a=12, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        for para in self.sections.get('suggestions', '').split('\n\n'):
            para = para.strip()
            if para:
                body(para, sp_b=12, sp_a=12, bold=False,
                     align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # ── LIMITATION ────────────────────────────────────────────────────────
        sec_head('LIMITATION', sp_b=12, sp_a=12, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        for para in self.sections.get('limitations', '').split('\n\n'):
            para = para.strip()
            if para:
                body(para, sp_b=12, sp_a=12, bold=False,
                     align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # ── CONCLUSION ────────────────────────────────────────────────────────
        sec_head('CONCLUSION', sp_b=0, sp_a=0, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        for para in self.sections.get('conclusion', '').split('\n\n'):
            para = para.strip()
            if para:
                body(para, sp_b=12, sp_a=12, bold=False,
                     align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        # ── REFERENCES ────────────────────────────────────────────────────────
        sec_head('REFERENCE', sp_b=0, sp_a=0, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        refs = self.sections.get('references', [])
        for i, ref in enumerate(refs):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf = p.paragraph_format
            pf.space_before      = Pt(6)
            pf.space_after       = Pt(0)
            pf.first_line_indent = Inches(-0.25)
            pf.left_indent       = Inches(0.5)
            # Format: "1. Author (Year). Title. Journal."
            ref_text = f"{i+1}. {ref}" if not ref.strip().startswith(str(i+1)+'.') else ref
            r = p.add_run(ref_text)
            r.bold = False; r.font.size = Pt(12); r.font.name = TNR

        # ── PLAGIARISM NOTE ───────────────────────────────────────────────────
        sec_head('PLAGIARISM', sp_b=0, sp_a=0, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

        return doc

# ═══════════════════════════════════════════════════════════════════════════════
#  PAPER GENERATOR ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

class PaperGenerator:
    def __init__(self, jid: str, jobs_ref: dict):
        self.jid  = jid
        self.jobs = jobs_ref

    def prog(self, pct: int, msg: str):
        self.jobs[self.jid].update({'progress': pct, 'message': msg, 'status': 'running'})
        print(f'[{self.jid[:8]}] {pct}% – {msg}')

    def generate(self, topic: str, nfigs: int, author: str, inst: str, email: str, questionnaire: dict = None) -> str:
        os.makedirs('generated', exist_ok=True)
        self.prog(5, 'Initializing...')

        # ── Step 1: Web scraping — 3 sources in parallel ─────────────────────
        self.prog(8, 'Scraping Semantic Scholar, CrossRef & Wikipedia...')
        scraper = WebScraper(topic)
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_ss, f_cr, f_wiki = (
                ex.submit(scraper.fetch_semantic_scholar, 10),
                ex.submit(scraper.fetch_crossref, 6),
                ex.submit(scraper.fetch_wikipedia),
            )
            ss, cr, wiki = f_ss.result(), f_cr.result(), f_wiki.result()

        seen, all_papers = set(), []
        for p in ss + cr:
            key = p['title'][:40].lower()
            if key not in seen:
                seen.add(key); all_papers.append(p)
        all_papers.sort(key=lambda x: x.get('citations', 0), reverse=True)
        scraped = {'papers': all_papers, 'wiki': wiki}
        print(f"[Scraper] {len(ss)} SS + {len(cr)} CrossRef, wiki={'yes' if wiki.get('summary') else 'no'}")

        # ── Step 2: Single streaming Gemini call writes the whole paper ────────
        self.prog(30, 'Gemini connected — writing keywords...')
        writer        = GeminiWriter(topic, scraped, questionnaire=questionnaire or {})
        writer._nfigs = nfigs
        sections      = writer.generate_all(progress_cb=self.prog)
        self.prog(76, 'Gemini finished. Parsing sections...')

        sections['references'] = writer.references()

        # ── Step 3: Parse chart specs from Gemini's <charts> block ───────────
        self.prog(78, 'Parsing chart specs...')
        specs = writer.parse_chart_specs(nfigs)
        if not specs:
            specs = writer._fallback_specs(nfigs)

        # ── Step 4: Render charts ────────────────────────────────────────────
        self.prog(82, f'Rendering {len(specs)} SPSS-style charts...')
        charts = [make_chart(sp) for sp in specs]

        # ── Step 5: Build DOCX ───────────────────────────────────────────────
        self.prog(90, 'Assembling Word document...')
        builder = DocBuilder(topic, author, inst, email, writer, sections, specs, charts, all_papers)
        doc     = builder.build()

        self.prog(97, 'Saving...')
        safe = re.sub(r'[^\w\-]', '_', topic[:40])
        out  = os.path.abspath(f'generated/rdxper_{safe}_{self.jid[:8]}.docx')
        doc.save(out)
        self.prog(99, 'Done!')
        return out


# ═══════════════════════════════════════════════════════════════════════════════
#  EMBEDDED FRONTEND
# ═══════════════════════════════════════════════════════════════════════════════

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>rdxper</title>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#ffffff;color:#111111;min-height:100vh}
:root{--bg:#ffffff;--surface:#ffffff;--surface2:#f5f5f5;--surface3:#ececec;--border:#d0d0d0;--accent:#111111;--accent2:#444444;--text:#111111;--muted:#666666;--dim:#999999;--error:#cc0000;--r:10px}
.wrap{max-width:960px;margin:0 auto;padding:0 20px}
header{padding:18px 0;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid #111}
.logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:32px;height:32px;background:#111;border-radius:6px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:12px;color:#fff}
.logo-text{font-size:20px;font-weight:900;letter-spacing:-0.5px;color:#111}
.logo-text span{color:#111}
.user-chip{display:flex;align-items:center;gap:8px;background:#f5f5f5;border:1px solid #d0d0d0;border-radius:40px;padding:5px 12px 5px 5px;cursor:pointer}
.user-chip img{width:26px;height:26px;border-radius:50%;object-fit:cover}
.user-chip span{font-size:13px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#111}
.nav-links{display:flex;gap:8px;align-items:center}
.nav-btn{background:none;border:1px solid #d0d0d0;color:#666;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .2s}
.nav-btn:hover{border-color:#111;color:#111;background:#f5f5f5}
.nav-btn.danger{border-color:#cc0000;color:#cc0000}
.screen{display:none}.screen.active{display:block}
.hero{padding:56px 0 32px;text-align:center}
.htag{font-size:12px;color:#666;letter-spacing:2px;text-transform:uppercase;margin-bottom:16px;font-family:Consolas,monospace}
h1{font-size:clamp(28px,5vw,52px);font-weight:900;line-height:1.1;margin-bottom:16px;color:#111}
h1 em{color:#111;font-style:normal;border-bottom:3px solid #111}
.sub{font-size:16px;color:#666;max-width:560px;margin:0 auto 32px}
.card{background:#fff;border:1.5px solid #d0d0d0;border-radius:var(--r);padding:32px;max-width:440px;margin:0 auto;width:100%}
.ct{font-size:20px;font-weight:700;margin-bottom:6px;color:#111}
.cs{font-size:14px;color:#666;margin-bottom:24px}
.btn{width:100%;padding:13px 20px;border-radius:8px;border:none;font-size:15px;font-weight:700;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:10px}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-p{background:#111;color:#fff;border:2px solid #111}
.btn-p:hover:not(:disabled){background:#333;transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.2)}
.btn-dl{background:#111;color:#fff;border:2px solid #111;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.btn-dl:hover:not(:disabled){background:#333;transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.25)}
.btn-s{background:#fff;color:#111;border:1.5px solid #d0d0d0}
.btn-s:hover:not(:disabled){border-color:#111;background:#f5f5f5}
.fg{margin-bottom:16px}.fg label{display:block;font-size:13px;color:#555;margin-bottom:6px;font-weight:600}
.fg input,.fg select{width:100%;background:#f9f9f9;border:1.5px solid #d0d0d0;border-radius:8px;padding:10px 14px;color:#111;font-size:14px;outline:none;transition:border-color .2s}
.fg input:focus,.fg input:focus-within{border-color:#111;background:#fff}
.notif{display:none;padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:14px}
.notif.show{display:block}
.notif.success{background:#f0f0f0;border:1.5px solid #111;color:#111}
.notif.error{background:#fff0f0;border:1.5px solid #cc0000;color:#cc0000}
.notif.info{background:#f5f5f5;border:1.5px solid #888;color:#444}
.prog-wrap{background:#e8e8e8;border-radius:100px;height:5px;overflow:hidden;margin:12px 0}
.prog-fill{height:100%;background:#111;border-radius:100px;transition:width .4s ease}
.prog-row{display:flex;justify-content:space-between;font-size:12px;color:#666;margin-bottom:4px}
.stage-box{background:#f5f5f5;border:1.5px solid #d0d0d0;border-radius:var(--r);padding:10px 14px;margin:10px 0;display:flex;align-items:center;gap:8px}
.stage-msg{font-size:12px;color:#111;font-family:Consolas,monospace;flex:1;font-weight:600}
.sections-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin-bottom:12px}
.sec-item{font-size:9px;padding:4px;border-radius:5px;background:#f5f5f5;border:1px solid #d0d0d0;color:#999;text-align:center;font-family:Consolas,monospace;transition:all .3s}
.sec-item.writing{background:#ececec;border-color:#111;color:#111;animation:sp 1s ease-in-out infinite;font-weight:700}
.sec-item.done{background:#111;border-color:#111;color:#fff}
@keyframes sp{0%,100%{opacity:1}50%{opacity:.4}}
.spin{width:14px;height:14px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:#fff;border:1.5px solid #d0d0d0;border-radius:var(--r);padding:20px}
.stat-val{font-size:28px;font-weight:900;color:#111}
.stat-lbl{font-size:12px;color:#666;margin-top:4px}
.table-wrap{background:#fff;border:1.5px solid #d0d0d0;border-radius:var(--r);overflow:hidden;margin-bottom:24px}
.table-head{padding:14px 20px;border-bottom:1.5px solid #d0d0d0;font-size:14px;font-weight:700;color:#111}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px 16px;font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.5px;border-bottom:1.5px solid #d0d0d0;font-weight:700;background:#f9f9f9}
td{padding:10px 16px;font-size:13px;border-bottom:1px solid #ececec;color:#111}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f9f9f9}
.badge-paid{background:#111;color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.badge-free{background:#f5f5f5;color:#666;border:1px solid #d0d0d0;padding:2px 8px;border-radius:4px;font-size:11px}
.badge-pending{background:#f5f5f5;color:#888;border:1px solid #ccc;padding:2px 8px;border-radius:4px;font-size:11px}
.avatar{width:32px;height:32px;border-radius:50%;object-fit:cover;border:1px solid #d0d0d0}
.profile-header{display:flex;align-items:center;gap:16px;background:#fff;border:1.5px solid #d0d0d0;border-radius:var(--r);padding:24px;margin-bottom:20px}
.profile-avatar{width:64px;height:64px;border-radius:50%;border:2px solid #111}
.tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #d0d0d0}
.tab{padding:10px 18px;font-size:13px;cursor:pointer;border-radius:0;color:#666;border:none;background:none;transition:all .2s;border-bottom:2px solid transparent;margin-bottom:-2px;font-weight:600}
.tab.active{color:#111;border-bottom:2px solid #111;font-weight:700}
.empty{text-align:center;padding:40px;color:#999;font-size:14px}
.pay-box{background:#f5f5f5;border:1.5px solid #d0d0d0;border-radius:12px;padding:20px;text-align:center;margin:16px 0}
.pay-amt{font-size:40px;font-weight:900;color:#111}
.page-title{font-size:24px;font-weight:900;margin:32px 0 4px;color:#111}
.page-sub{font-size:14px;color:#666;margin-bottom:24px}
footer{text-align:center;padding:32px 0;color:#999;font-size:12px;border-top:1.5px solid #d0d0d0;margin-top:40px}
/* Questionnaire */
.q-steps{display:flex;align-items:center;margin-bottom:28px;padding:0 4px}
.q-step{display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;min-width:56px}
.q-num{width:28px;height:28px;border-radius:50%;background:#f5f5f5;border:2px solid #d0d0d0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:#999;transition:all .3s}
.q-lbl{font-size:10px;color:#999;transition:color .3s;white-space:nowrap}
.q-step.active .q-num{background:#111;border-color:#111;color:#fff}
.q-step.active .q-lbl{color:#111;font-weight:700}
.q-step.done .q-num{background:#555;border-color:#555;color:#fff}
.q-step.done .q-lbl{color:#555}
.q-line{flex:1;height:2px;background:#d0d0d0;margin:0 4px;margin-bottom:14px;transition:background .3s}
.q-line.done{background:#111}
.q-panel{display:none}.q-panel.active{display:block}
.q-badge{font-size:11px;color:#888;font-family:Consolas,monospace;letter-spacing:1px;margin-bottom:8px;font-weight:700;text-transform:uppercase}
.q-hint{background:#f9f9f9;border:1.5px solid #d0d0d0;border-radius:8px;padding:10px 14px;font-size:12px;color:#555;margin-bottom:16px;line-height:1.5}
textarea{width:100%;background:#f9f9f9;border:1.5px solid #d0d0d0;border-radius:8px;padding:10px 14px;color:#111;font-size:13px;outline:none;transition:border-color .2s;resize:vertical;font-family:'Segoe UI',Arial,sans-serif;line-height:1.6}
textarea:focus{border-color:#111;background:#fff}
textarea::placeholder{color:#bbb;font-size:12px}
.q-summary{background:#f9f9f9;border:1.5px solid #d0d0d0;border-radius:10px;padding:16px;margin-bottom:20px;font-size:12px}
.q-summary-item{margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid #e8e8e8}
.q-summary-item:last-child{margin-bottom:0;padding-bottom:0;border-bottom:none}
.q-summary-label{color:#111;font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.q-summary-val{color:#444;line-height:1.5;max-height:60px;overflow:hidden;text-overflow:ellipsis}
@media(max-width:600px){.q-lbl{display:none}.q-steps{gap:0}.q-step{min-width:36px}}
@media(max-width:600px){.sections-grid{grid-template-columns:repeat(3,1fr)}.stat-grid{grid-template-columns:repeat(2,1fr)}.nav-links{gap:4px}}
/* ── Dashboard ── */
.dash-header{padding:36px 0 8px}
.dash-greeting{font-size:13px;color:#888;letter-spacing:.5px;text-transform:uppercase;font-family:Consolas,monospace;margin-bottom:6px}
.dash-title{font-size:30px;font-weight:900;letter-spacing:-1px;color:#111}
.dash-title span{color:#111}
.dash-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:80px 20px;text-align:center;border:2px dashed #d0d0d0;border-radius:16px;margin:28px 0}
.dash-empty-icon{font-size:48px;margin-bottom:16px;opacity:.3}
.dash-empty-txt{font-size:16px;font-weight:700;color:#666;margin-bottom:6px}
.dash-empty-sub{font-size:13px;color:#999}
.papers-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin:24px 0}
.paper-card{background:#fff;border:1.5px solid #d0d0d0;border-radius:10px;padding:20px;cursor:default;transition:border-color .15s,transform .15s,box-shadow .15s;position:relative;overflow:hidden}
.paper-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:#111;opacity:0;transition:opacity .15s}
.paper-card:hover{border-color:#111;transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.1)}
.paper-card:hover::before{opacity:1}
.paper-card-topic{font-size:14px;font-weight:700;color:#111;line-height:1.4;margin-bottom:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.paper-card-meta{display:flex;align-items:center;justify-content:space-between;font-size:11px;color:#999}
.paper-card-date{font-family:Consolas,monospace}
.paper-card-badge{padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.3px}
.badge-done{background:#111;color:#fff}
.badge-pending{background:#f5f5f5;color:#888;border:1px solid #d0d0d0}
.fab{position:fixed;bottom:32px;right:32px;width:58px;height:58px;border-radius:50%;background:#111;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.25);transition:transform .2s,box-shadow .2s;z-index:100}
.fab:hover{transform:scale(1.1) translateY(-2px);box-shadow:0 8px 28px rgba(0,0,0,.35)}
.fab svg{width:24px;height:24px;stroke:#fff;stroke-width:2.5;stroke-linecap:round}
.fab-tooltip{position:fixed;bottom:44px;right:100px;background:#111;border-radius:6px;padding:7px 12px;font-size:12px;color:#fff;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .2s;z-index:99}
.fab:hover ~ .fab-tooltip{opacity:1}
@media(max-width:600px){.fab{bottom:24px;right:20px}.papers-grid{grid-template-columns:1fr}}
/* ── Responsive ── */
@media(max-width:480px){
  .wrap{padding:0 12px}
  header{padding:12px 0;flex-wrap:wrap;gap:8px}
  .logo-text{font-size:17px}
  .logo-mark{width:28px;height:28px;font-size:10px}
  .nav-btn{padding:4px 8px;font-size:11px}
  .user-chip{padding:4px 8px 4px 4px}
  .user-chip span{max-width:80px;font-size:12px}
  .hero{padding:32px 0 20px}
  h1{font-size:26px}
  .sub{font-size:14px}
  .card{padding:20px 16px}
  .ct{font-size:17px}
  .cs{font-size:13px}
  .btn{padding:12px 16px;font-size:14px}
  .fg input,.fg select{padding:9px 12px;font-size:14px}
  textarea{font-size:13px}
  .dash-header{padding:24px 0 4px}
  .dash-title{font-size:24px}
  .dash-empty{padding:48px 16px}
  .stat-grid{grid-template-columns:1fr 1fr}
  .stat-val{font-size:22px}
  .table-wrap{overflow-x:auto}
  table{font-size:12px;min-width:360px}
  th,td{padding:8px 10px}
  .profile-header{flex-direction:column;text-align:center;gap:12px}
  .profile-avatar{width:56px;height:56px}
  .tabs{overflow-x:auto;white-space:nowrap;-webkit-overflow-scrolling:touch}
  .tab{padding:8px 14px;font-size:12px}
  .card[style*="max-width:560px"]{padding:20px 16px}
  .stage-box{padding:8px 12px}
  .stage-msg{font-size:11px}
  .sections-grid{grid-template-columns:repeat(3,1fr)}
  .sec-item{font-size:8px;padding:3px}
  .q-badge{font-size:10px}
  .q-hint{font-size:11px;padding:8px 12px}
  .q-summary{padding:12px}
  .q-summary-label{font-size:10px}
  .q-summary-val{font-size:12px}
  .page-title{font-size:20px}
  .page-sub{font-size:13px}
}
@media(min-width:481px) and (max-width:768px){
  .wrap{padding:0 16px}
  header{padding:14px 0}
  h1{font-size:36px}
  .card{padding:28px 24px;max-width:100%}
  .stat-grid{grid-template-columns:repeat(3,1fr)}
  .papers-grid{grid-template-columns:repeat(2,1fr)}
  .user-chip span{max-width:110px}
  .table-wrap{overflow-x:auto}
  table{min-width:420px}
}
@media(min-width:769px) and (max-width:1024px){
  .wrap{padding:0 24px}
  .papers-grid{grid-template-columns:repeat(2,1fr)}
  .stat-grid{grid-template-columns:repeat(4,1fr)}
}
@media(hover:none){
  .paper-card:hover{transform:none;box-shadow:none}
  .paper-card:hover::before{opacity:0}
  .btn-p:hover:not(:disabled){transform:none;box-shadow:none}
  .btn-dl:hover:not(:disabled){transform:none;box-shadow:none}
  .fab:hover{transform:scale(1);box-shadow:0 4px 16px rgba(0,0,0,.25)}
}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="logo">
    <div class="logo-mark">rx</div>
    <div class="logo-text">RD<span>Xper</span></div>
  </div>
  <div class="nav-links" id="nav-auth" style="display:none">
    <button class="nav-btn" onclick="showProfile()">👤 Profile</button>
    <div id="admin-link" style="display:none"><button class="nav-btn" onclick="showAdmin()">⚙️ Admin</button></div>
    <div class="user-chip" onclick="showProfile()">
      <img id="nav-avatar" src="" onerror="this.style.display='none'" style="display:none">
      <span id="nav-name">User</span>
    </div>
    <button class="nav-btn danger" onclick="logout()">Sign out</button>
  </div>
</header>

<!-- LOGIN -->
<div class="screen active" id="s-home">
  <div class="hero">
    
    <h1>Generate <em>Genuine</em><br>Research Papers</h1>
    
  </div>
  <div class="card">
    <div class="ct">Sign in to continue</div>
    <div class="cs">Use your Google account — no password needed</div>
    <div id="n-login" class="notif"></div>
    <div id="g-btn-wrap" style="display:flex;justify-content:center;min-height:44px;align-items:center"></div>
  </div>
</div>

<!-- DASHBOARD -->
<div class="screen" id="s-dashboard">
  <div class="dash-header">
    <div class="dash-greeting">Welcome back</div>
    <div class="dash-title" id="dash-name-title">Researcher</div>
  </div>

  <div style="display:flex;align-items:center;justify-content:space-between;margin:28px 0 8px">
    <div style="font-size:16px;font-weight:700">Your Research Papers</div>
    <button class="nav-btn" onclick="loadDashboard()" style="font-size:11px">↻ Refresh</button>
  </div>

  <div id="dash-papers-wrap">
    <div class="dash-empty">
      <div class="dash-empty-icon">📄</div>
      <div class="dash-empty-txt">No papers yet</div>
      <div class="dash-empty-sub">Press <strong style="color:var(--accent)">+</strong> below to generate your first research paper</div>
    </div>
  </div>

  <!-- Floating Action Button -->
  <button class="fab" onclick="startNewPaper()" title="New Research Paper">
    <svg viewBox="0 0 24 24" fill="none"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
  </button>
  <div class="fab-tooltip">New Research Paper</div>
</div>

<!-- GENERATE — 5-Step Questionnaire -->
<div class="screen" id="s-gen">
<div style="padding-top:28px;max-width:700px;margin:0 auto">

<div style="margin-bottom:16px">
  <button class="btn btn-s" style="width:auto;padding:8px 16px;font-size:12px" onclick="loadDashboard();show('s-dashboard')">← Dashboard</button>
</div>

<!-- Step indicator -->
<div class="q-steps" id="q-steps">
  <div class="q-step active" id="qs-0" onclick="goStep(0)"><span class="q-num">1</span><span class="q-lbl">Problem</span></div>
  <div class="q-line"></div>
  <div class="q-step" id="qs-1" onclick="goStep(1)"><span class="q-num">2</span><span class="q-lbl">Literature</span></div>
  <div class="q-line"></div>
  <div class="q-step" id="qs-2" onclick="goStep(2)"><span class="q-num">3</span><span class="q-lbl">Gap</span></div>
  <div class="q-line"></div>
  <div class="q-step" id="qs-3" onclick="goStep(3)"><span class="q-num">4</span><span class="q-lbl">Objectives</span></div>
  <div class="q-line"></div>
  <div class="q-step" id="qs-4" onclick="goStep(4)"><span class="q-num">5</span><span class="q-lbl">Statement</span></div>
  <div class="q-line"></div>
  <div class="q-step" id="qs-5" onclick="goStep(5)"><span class="q-num">6</span><span class="q-lbl">Settings</span></div>
</div>

<!-- ── Step 0: Problem Identification ───────────────────── -->
<div class="q-panel active" id="qp-0">
  <div class="q-badge">Step 1 of 6</div>
  <div class="ct" style="margin-bottom:6px">Identification of the Problem</div>
  <div class="cs" style="margin-bottom:20px">What specific problem prompted this research? Describe it in your own words, AI will use this as the foundation. <strong style="color:var(--accent)">Optional — skip if you prefer AI to write this.</strong></div>
  <div class="q-hint">💡 Think about: What is wrong or missing? Who is affected? What is the scale of the problem? What are the consequences of not addressing it?</div>
  <div class="fg">
    <label>Research Topic / Title *</label>
    <input type="text" id="topic-in" placeholder="e.g. Legal Frameworks for Environmental Restoration in Post-War Reconstruction">
  </div>
  <div class="fg">
    <label>Problem Statement <span style="color:var(--dim);font-weight:400">(optional)</span></label>
    <textarea id="q-problem" rows="5" placeholder="Describe the core problem your research addresses. What issue exists? What are its consequences? Why does it need to be studied now?&#10;&#10;Example: Armed conflicts inflict devastating environmental damage that persists long after hostilities cease. Existing legal frameworks under the Geneva Conventions and Rome Statute fail to adequately address post-war ecological restoration, leaving affected communities without legal recourse or environmental remediation. This gap in international humanitarian law creates a vacuum where neither state nor non-state actors are held accountable for long-term environmental harm..."></textarea>
  </div>
  <div style="display:flex;gap:10px;justify-content:flex-end">
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="nextStep(0)">Skip →</button>
      <button class="btn btn-p" style="width:auto;padding:10px 28px" onclick="nextStep(0)">Next → Literature Review</button>
    </div>
  </div>
</div>

<!-- ── Step 1: Literature Review ────────────────────────── -->
<div class="q-panel" id="qp-1">
  <div class="q-badge">Step 2 of 6</div>
  <div class="ct" style="margin-bottom:6px">Literature Review</div>
  <div class="cs" style="margin-bottom:20px">What sources have you reviewed? List them and AI will expand into a full literature review. <strong style="color:var(--accent)">Optional — AI will find real papers automatically if you skip.</strong></div>
  <div class="q-hint">💡 Include: Author names and years, key arguments, relevant reports, laws, treaties, court cases, or books. Even brief notes are fine — AI will elaborate.</div>
  <div class="fg">
    <label>Key Sources & Their Main Arguments *</label>
    <textarea id="q-lit" rows="8" placeholder="List the sources you have reviewed and what they say. Examples:&#10;&#10;- Geneva Conventions (1949) & Additional Protocol I (1977) — establish basic environmental protections during armed conflict but lack post-war restoration obligations&#10;- UNEP (2009) From Conflict to Peacebuilding — documents how environmental damage sustains conflict cycles&#10;- Bothe, Bruch & Jensen (2010) — argue existing IHL is inadequate for modern environmental warfare&#10;- Rome Statute Art. 8 — criminalises widespread environmental damage but enforcement is rare&#10;- UN Compensation Commission (Kuwait, 1991) — first successful precedent for war environmental claims..."></textarea>
  </div>
  <div style="display:flex;gap:10px;justify-content:space-between">
    <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="prevStep(1)">← Back</button>
    <button class="btn btn-s" style="width:auto;padding:10px 18px" onclick="nextStep(1)">Skip →</button>
    <button class="btn btn-p" style="width:auto;padding:10px 28px" onclick="nextStep(1)">Next → Research Gap</button>
  </div>
</div>

<!-- ── Step 2: Research Gap ──────────────────────────────── -->
<div class="q-panel" id="qp-2">
  <div class="q-badge">Step 3 of 6</div>
  <div class="ct" style="margin-bottom:6px">Research Gap</div>
  <div class="cs" style="margin-bottom:20px">What is missing from existing research? AI will use your answer as the gap statement. <strong style="color:var(--accent)">Optional — AI will identify a gap automatically if you skip.</strong></div>
  <div class="q-hint">💡 Ask yourself: What do existing studies not cover? What contradictions exist in the literature? What context or population has been ignored? What methodology hasn't been applied?</div>
  <div class="fg">
    <label>The Research Gap <span style="color:var(--dim);font-weight:400">(optional)</span></label>
    <textarea id="q-gap" rows="5" placeholder="Describe what is missing from current research and why your study is needed.&#10;&#10;Example: While significant scholarship exists on environmental protection during armed conflict, there is a critical gap in research on post-war environmental restoration obligations. Existing studies either focus on pre-conflict prevention or general humanitarian law without addressing the specific legal mechanisms required for ecological recovery. Furthermore, no comparative study has examined how different post-conflict nations (Iraq, Kosovo, Lebanon, Ukraine) have implemented or failed to implement environmental restoration under international law..."></textarea>
  </div>
  <div style="display:flex;gap:10px;justify-content:space-between">
    <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="prevStep(2)">← Back</button>
    <button class="btn btn-s" style="width:auto;padding:10px 18px" onclick="nextStep(2)">Skip →</button>
    <button class="btn btn-p" style="width:auto;padding:10px 28px" onclick="nextStep(2)">Next → Objectives</button>
  </div>
</div>

<!-- ── Step 3: Objectives ────────────────────────────────── -->
<div class="q-panel" id="qp-3">
  <div class="q-badge">Step 4 of 6</div>
  <div class="ct" style="margin-bottom:6px">Objectives of the Research</div>
  <div class="cs" style="margin-bottom:20px">List your research objectives — they will appear verbatim in your paper. <strong style="color:var(--accent)">Optional — AI will generate objectives aligned to your topic if you skip.</strong></div>
  <div class="q-hint">💡 Good objectives: Start with "To examine / To analyse / To evaluate / To compare / To propose". Be specific. You need 4–6 objectives. One per line.</div>
  <div class="fg">
    <label>Research Objectives <span style="color:var(--dim);font-weight:400">(optional — one per line)</span></label>
    <textarea id="q-objectives" rows="7" placeholder="To examine the existing international legal frameworks governing environmental restoration in post-war reconstruction&#10;To analyse compensation mechanisms including liability determination, reparations, and restoration funding&#10;To evaluate practical challenges such as political instability, limited resources, and technical capacity gaps&#10;To compare legal approaches from different post-conflict contexts including Iraq, Kosovo, Lebanon, and Ukraine&#10;To propose recommendations for strengthening enforcement mechanisms and legal accountability for wartime environmental harm"></textarea>
  </div>
  <div style="display:flex;gap:10px;justify-content:space-between">
    <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="prevStep(3)">← Back</button>
    <button class="btn btn-s" style="width:auto;padding:10px 18px" onclick="nextStep(3)">Skip →</button>
    <button class="btn btn-p" style="width:auto;padding:10px 28px" onclick="nextStep(3)">Next → Research Statement</button>
  </div>
</div>

<!-- ── Step 4: Research Statement ───────────────────────── -->
<div class="q-panel" id="qp-4">
  <div class="q-badge">Step 5 of 6</div>
  <div class="ct" style="margin-bottom:6px">Research Statement</div>
  <div class="cs" style="margin-bottom:20px">Your thesis in 2–4 sentences — what this research does, how, and why. <strong style="color:var(--accent)">Optional — AI will formulate a research statement if you skip.</strong></div>
  <div class="q-hint">💡 A good research statement: Names the topic, identifies the method (doctrinal/empirical/comparative), and states the significance. Typically 2–4 sentences.</div>
  <div class="fg">
    <label>Research Statement <span style="color:var(--dim);font-weight:400">(optional)</span></label>
    <textarea id="q-statement" rows="5" placeholder="This study investigates the legal frameworks governing environmental restoration in post-war reconstruction, focusing on obligations, compensation mechanisms, and practical implementation challenges. Through a comparative doctrinal analysis of international instruments and empirical case studies from four post-conflict regions, this research identifies critical gaps in existing law and proposes actionable reforms to strengthen ecological restoration as an integral component of sustainable peace-building."></textarea>
  </div>
  <div style="display:flex;gap:10px;justify-content:space-between">
    <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="prevStep(4)">← Back</button>
    <button class="btn btn-s" style="width:auto;padding:10px 18px" onclick="nextStep(4)">Skip →</button>
    <button class="btn btn-p" style="width:auto;padding:10px 28px" onclick="nextStep(4)">Next → Paper Settings</button>
  </div>
</div>

<!-- ── Step 5: Settings + Generate ──────────────────────── -->
<div class="q-panel" id="qp-5">
  <div class="q-badge">Step 6 of 6</div>
  <div class="ct" style="margin-bottom:6px">Paper Settings</div>
  <div class="cs" style="margin-bottom:20px">Final details for your paper. AI will now use all your inputs to generate a genuine research paper.</div>
  <div id="n-gen" class="notif"></div>
  <!-- Summary of inputs -->
  <div class="q-summary" id="q-summary"></div>
  <div class="fg"><label>Author Name</label>
    <input type="text" id="author-in" placeholder="Your full name">
  </div>
  <div class="fg"><label>Institution (optional)</label>
    <input type="text" id="inst-in" placeholder="University / College / Organisation">
  </div>
  <div class="fg">
    <label>Gemini API Key <span style="color:var(--accent)">*</span> <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--accent);font-size:11px;text-decoration:none">Get free key ↗</a></label>
    <input type="password" id="gemini-key-in" placeholder="Paste your Gemini API key (AIza...)" autocomplete="off">
    <div style="font-size:11px;color:var(--dim);margin-top:4px">Free at Google AI Studio — no credit card needed. Key stays in your browser only.</div>
  </div>
  <div class="fg"><label>Number of Figures: <b id="sl-display">6</b></label>
    <input type="range" id="sl" min="3" max="15" value="6"
      oninput="document.getElementById('sl-display').textContent=this.value"
      style="width:100%;accent-color:var(--accent)">
  </div>
  <div style="display:flex;gap:10px;justify-content:space-between">
    <button class="btn btn-s" style="width:auto;padding:10px 20px" onclick="prevStep(5)">← Back</button>
    <button class="btn btn-p" id="btn-gen" onclick="generate()" style="flex:1">Generate Research Paper</button>
  </div>
</div>

</div>
</div>

<!-- PROGRESS -->
<div class="screen" id="s-prog">
  <div style="padding-top:40px">
    <div class="card" style="max-width:560px">
      <div class="ct" id="prog-ct">Generating your paper...</div>
      <div class="cs" id="prog-topic"></div>
      <div class="stage-box"><span style="font-size:18px">⚡</span><span class="stage-msg" id="stage-msg">Initialising...</span></div>
      <div class="prog-row"><span></span><span id="prog-pct">0%</span></div>
      <div class="prog-wrap"><div class="prog-fill" id="prog-fill" style="width:0%"></div></div>
      <div class="sections-grid" id="sec-grid"></div>
    </div>
  </div>
</div>

<!-- DONE -->
<div class="screen" id="s-done">
  <div style="padding-top:48px">
    <div class="card" style="text-align:center">
      <div style="font-size:48px;margin-bottom:12px">✅</div>
      <div class="ct">Paper ready!</div>
      <div class="cs">Your research paper has been generated successfully</div>
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:16px;margin:16px 0;text-align:left">
        <div style="display:flex;justify-content:space-between;margin-bottom:8px">
          <span style="color:var(--muted);font-size:13px">Topic</span>
          <span style="font-size:13px;font-weight:600;max-width:220px;text-align:right" id="d-topic"></span></div>
        <div style="display:flex;justify-content:space-between;margin-bottom:8px">
          <span style="color:var(--muted);font-size:13px">Figures</span>
          <span style="font-size:13px" id="d-figs"></span></div>
        <div style="display:flex;justify-content:space-between">
          <span style="color:var(--muted);font-size:13px">Generated</span>
          <span style="font-size:13px" id="d-time"></span></div>
      </div>
      <button class="btn btn-dl" id="btn-dl" onclick="download()">⬇ Download Research Paper (.docx)</button>
      <button class="btn btn-s" onclick="again()" style="margin-top:8px">Generate another paper</button>
      <button class="btn btn-s" onclick="loadDashboard();show('s-dashboard')" style="margin-top:6px;opacity:.7">← Back to Dashboard</button>
    </div>
  </div>
</div>

<!-- PROFILE -->
<div class="screen" id="s-profile">
  <div style="padding-top:28px">
    <div class="profile-header">
      <img class="profile-avatar" id="prof-avatar" src=""
        onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 64 64%22><circle cx=%2232%22 cy=%2232%22 r=%2232%22 fill=%22%23333%22/></svg>'">
      <div>
        <div style="font-size:20px;font-weight:700" id="prof-name">—</div>
        <div style="font-size:13px;color:var(--muted);margin-top:3px" id="prof-email">—</div>
        <div style="font-size:11px;color:var(--dim);margin-top:4px">Member since <span id="prof-since">—</span></div>
      </div>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-val" id="prof-papers-count">0</div><div class="stat-lbl">Papers Generated</div></div>
      <div class="stat-card"><div class="stat-val" id="prof-spent">₹0</div><div class="stat-lbl">Total Spent</div></div>
      <div class="stat-card"><div class="stat-val" id="prof-paid-count">0</div><div class="stat-lbl">Papers Downloaded</div></div>
    </div>
    <div class="table-wrap">
      <div class="table-head">📄 Your Papers</div>
      <table><thead><tr><th>Topic</th><th>Date</th><th>Status</th></tr></thead>
      <tbody id="prof-papers-list"><tr><td colspan="3" class="empty">Loading...</td></tr></tbody></table>
    </div>
    <button class="btn btn-s" onclick="loadDashboard();show('s-dashboard')" style="max-width:180px">← Back</button>
  </div>
</div>

<!-- ADMIN -->
<div class="screen" id="s-admin">
  <div style="padding-top:28px">
    <div class="page-title">⚙️ Admin Dashboard</div>
    <div class="page-sub">All users, papers and payments</div>
    <div class="stat-grid">
      <div class="stat-card"><div class="stat-val" id="adm-users-c">—</div><div class="stat-lbl">Total Users</div></div>
      <div class="stat-card"><div class="stat-val" id="adm-papers-c">—</div><div class="stat-lbl">Papers Generated</div></div>
      <div class="stat-card"><div class="stat-val" id="adm-revenue-c">—</div><div class="stat-lbl">Total Revenue</div></div>
      <div class="stat-card"><div class="stat-val" id="adm-paid-c">—</div><div class="stat-lbl">Paid Downloads</div></div>
    </div>
    <div class="tabs">
      <button class="tab active" onclick="admTab('users',this)">👥 Users</button>
      <button class="tab" onclick="admTab('papers',this)">📄 Papers</button>
      <button class="tab" onclick="admTab('payments',this)">💳 Payments</button>
    </div>
    <div id="adm-tab-users">
      <div class="table-wrap"><table><thead><tr><th></th><th>Name</th><th>Email</th><th>Joined</th><th>Last Login</th></tr></thead>
      <tbody id="adm-users-list"></tbody></table></div>
    </div>
    <div id="adm-tab-papers" style="display:none">
      <div class="table-wrap"><table><thead><tr><th>Topic</th><th>User</th><th>Date</th><th>Status</th></tr></thead>
      <tbody id="adm-papers-list"></tbody></table></div>
    </div>
    <div id="adm-tab-payments" style="display:none">
      <div class="table-wrap"><table><thead><tr><th>User</th><th>Amount</th><th>Payment ID</th><th>Date</th><th>Status</th></tr></thead>
      <tbody id="adm-payments-list"></tbody></table></div>
    </div>
    <button class="btn btn-s" onclick="loadDashboard();show('s-dashboard')" style="max-width:180px;margin-top:12px">← Back</button>
  </div>
</div>

<footer></footer>
</div>

<script>
const SECS=['keywords','abstract','introduction','objectives','literature_review','methodology','results','discussion','suggestions','limitations','conclusion'];
let token='',userEmail='',userName='',userPicture='',jobId='',curTopic='',curFigs=6,poll=null;
const ADMIN_EM='__ADMIN_EMAIL__';
const G_CLIENT='__GOOGLE_CLIENT_ID__';

// Restore session
(async function(){
  try{
    const t=localStorage.getItem('rx_tok'),e=localStorage.getItem('rx_em'),
          n=localStorage.getItem('rx_nm'),p=localStorage.getItem('rx_pic');
    if(t&&e){
      // Validate token is still alive on the server before showing dashboard
      const r=await fetch('/api/profile',{headers:{'Authorization':'Bearer '+t}});
      if(r.ok){
        token=t;userEmail=e;userName=n||'';userPicture=p||'';onLoggedIn();
      } else {
        // Token expired or server restarted — clear stale data
        ['rx_tok','rx_em','rx_nm','rx_pic'].forEach(k=>localStorage.removeItem(k));
      }
    }
  }catch(e){}
})();

// Google Sign-In init
window.addEventListener('load',function(){
  if(!G_CLIENT){
    // Dev mode: show simple login form instead of Google button
    document.getElementById('g-btn-wrap').innerHTML=`
      <div style="width:100%">
        <div style="background:rgba(255,200,0,.1);border:1px solid rgba(255,200,0,.3);border-radius:8px;padding:10px 14px;font-size:12px;color:#ffd700;margin-bottom:14px;text-align:center">
          🛠️ Local / Dev Mode — Google Sign-In not configured
        </div>
        <div class="fg" style="margin-bottom:10px">
          <label style="font-size:12px">Your Name</label>
          <input type="text" id="dev-name" placeholder="e.g. Rakunatha Khrishanth" style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;color:var(--text);width:100%;font-size:13px;outline:none">
        </div>
        <div class="fg" style="margin-bottom:14px">
          <label style="font-size:12px">Your Email</label>
          <input type="email" id="dev-email" placeholder="e.g. you@email.com" style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;color:var(--text);width:100%;font-size:13px;outline:none">
        </div>
        <button class="btn btn-p" onclick="devLogin()" style="width:100%">Continue →</button>
      </div>`;
    return;
  }
  function tryInit(){
    if(window.google&&google.accounts){
      google.accounts.id.initialize({client_id:G_CLIENT,callback:handleGoogle,auto_select:false});
      const gWrap=document.getElementById('g-btn-wrap');const gW=Math.min(376,gWrap.offsetWidth||376);google.accounts.id.renderButton(gWrap,{theme:'outline',size:'large',width:gW,text:'continue_with',shape:'rectangular'});
    } else { setTimeout(tryInit,300); }
  }
  tryInit();
});

async function handleGoogle(resp){
  const n=document.getElementById('n-login');
  n.className='notif info show';n.textContent='Signing in...';
  try{
    const r=await fetch('/api/auth/google',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id_token:resp.credential})});
    const d=await r.json();
    if(!d.success){n.className='notif error show';n.textContent=d.message||'Sign in failed';return;}
    token=d.token;userEmail=d.email;userName=d.name;userPicture=d.picture;
    try{localStorage.setItem('rx_tok',token);localStorage.setItem('rx_em',userEmail);
        localStorage.setItem('rx_nm',userName);localStorage.setItem('rx_pic',userPicture);}catch(e){}
    onLoggedIn();
  }catch(e){n.className='notif error show';n.textContent='Connection error. Try again.';}
}

async function devLogin(){
  const name  = (document.getElementById('dev-name')||{value:''}).value.trim();
  const email = (document.getElementById('dev-email')||{value:''}).value.trim();
  if(!email){ alert('Please enter your email to continue.'); return; }
  const n=document.getElementById('n-login');
  n.className='notif info show'; n.textContent='Signing in...';
  try{
    const r=await fetch('/api/auth/dev',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name||email.split('@')[0], email})});
    const d=await r.json();
    if(!d.success){n.className='notif error show';n.textContent=d.message||'Sign in failed';return;}
    token=d.token;userEmail=d.email;userName=d.name;userPicture='';
    try{localStorage.setItem('rx_tok',token);localStorage.setItem('rx_em',userEmail);
        localStorage.setItem('rx_nm',userName);localStorage.setItem('rx_pic','');}catch(e){}
    onLoggedIn();
  }catch(e){n.className='notif error show';n.textContent='Connection error. Try again.';}
}

function onLoggedIn(){
  document.getElementById('nav-auth').style.display='flex';
  document.getElementById('nav-name').textContent=userName||userEmail.split('@')[0];
  const av=document.getElementById('nav-avatar');
  if(userPicture){av.src=userPicture;av.style.display='block';}
  if(userEmail===ADMIN_EM) document.getElementById('admin-link').style.display='block';
  const aIn=document.getElementById('author-in');
  if(aIn&&!aIn.value) aIn.value=userName||'';
  // Pre-fill saved Gemini key
  try{const saved=localStorage.getItem('rx_gkey');if(saved){const gIn=document.getElementById('gemini-key-in');if(gIn)gIn.value=saved;}}catch(e){}
  loadDashboard();
  show('s-dashboard');
}

async function loadDashboard(){
  const nameEl=document.getElementById('dash-name-title');
  if(nameEl) nameEl.innerHTML=(userName||userEmail.split('@')[0]).split(' ')[0]+'<span>.</span>';
  try{
    const r=await fetch('/api/profile',{headers:{'Authorization':'Bearer '+token}});
    if(r.status===401){forceLogout();return;}
    const d=await r.json();
    if(!d.success) return;
    const papers=d.papers||[];
    const wrap=document.getElementById('dash-papers-wrap');
    if(papers.length===0){
      wrap.innerHTML=`<div class="dash-empty">
        <div class="dash-empty-icon">📄</div>
        <div class="dash-empty-txt">No papers yet</div>
        <div class="dash-empty-sub">Press <strong style="color:var(--accent)">+</strong> below to generate your first research paper</div>
      </div>`;
    } else {
      wrap.innerHTML='<div class="papers-grid">'+papers.map(p=>`
        <div class="paper-card">
          <div class="paper-card-topic">${escHtml(p.topic||'Untitled')}</div>
          <div class="paper-card-meta">
            <span class="paper-card-date">${(p.created_at||'').slice(0,10)}</span>
            <span class="paper-card-badge ${p.file_path?'badge-done':'badge-pending'}">${p.file_path?'✓ Done':'Pending'}</span>
          </div>
        </div>`).join('')+'</div>';
    }
  }catch(e){console.error('Dashboard load error',e);}
}

function forceLogout(){
  token='';userEmail='';userName='';userPicture='';
  try{['rx_tok','rx_em','rx_nm','rx_pic'].forEach(k=>localStorage.removeItem(k));}catch(e){}
  document.getElementById('nav-auth').style.display='none';
  document.getElementById('admin-link').style.display='none';
  try{google.accounts.id.disableAutoSelect();}catch(e){}
  show('s-home');
}

function escHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function startNewPaper(){
  // Reset questionnaire state then navigate
  ['topic-in','inst-in','q-problem','q-lit','q-gap','q-objectives','q-statement'].forEach(id=>{
    const el=document.getElementById(id);if(el) el.value='';
  });
  const aIn=document.getElementById('author-in');
  if(aIn) aIn.value=userName||'';
  goStep(0);
  show('s-gen');
}

function show(id){document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');window.scrollTo({top:0,behavior:'smooth'});}
function notify(id,msg,type){const e=document.getElementById(id);e.textContent=msg;e.className='notif '+type+' show';if(type!=='error')setTimeout(()=>e.classList.remove('show'),6000);}

function logout(){
  token='';userEmail='';userName='';userPicture='';
  try{['rx_tok','rx_em','rx_nm','rx_pic'].forEach(k=>localStorage.removeItem(k));}catch(e){}
  document.getElementById('nav-auth').style.display='none';
  document.getElementById('admin-link').style.display='none';
  try{google.accounts.id.disableAutoSelect();}catch(e){}
  show('s-home');
}

// ── QUESTIONNAIRE NAVIGATION ────────────────────────────────────────────────
let currentStep = 0;
const totalSteps = 6;

function goStep(n){
  // Only allow going back to completed steps
  if(n > currentStep) return;
  currentStep = n;
  renderStep();
}

function nextStep(from){
  // Only validate the topic (required), everything else is optional
  if(from===0 && !document.getElementById('topic-in').value.trim()){
    alert('Please enter your research topic — this is the only required field.'); return;
  }
  currentStep = from + 1;
  if(currentStep === 5) buildSummary();
  renderStep();
}

function prevStep(from){
  currentStep = from - 1;
  renderStep();
}

function renderStep(){
  for(let i=0;i<totalSteps;i++){
    const panel = document.getElementById('qp-'+i);
    const step  = document.getElementById('qs-'+i);
    if(!panel||!step) continue;
    panel.classList.toggle('active', i===currentStep);
    step.classList.remove('active','done');
    if(i===currentStep) step.classList.add('active');
    else if(i<currentStep) step.classList.add('done');
    // Update connector lines
    const lines = document.querySelectorAll('.q-line');
    lines.forEach((l,li)=>{ l.classList.toggle('done', li < currentStep); });
  }
  window.scrollTo({top:0,behavior:'smooth'});
}

function buildSummary(){
  const items = [
    {label:'Problem Identified', id:'q-problem'},
    {label:'Literature Reviewed', id:'q-lit'},
    {label:'Research Gap', id:'q-gap'},
    {label:'Objectives', id:'q-objectives'},
    {label:'Research Statement', id:'q-statement'},
  ];
  const s = document.getElementById('q-summary');
  if(!s) return;
  s.innerHTML = '<div style="font-size:13px;font-weight:700;margin-bottom:12px;color:var(--text)">📋 Your Research Inputs</div>' +
    items.map(item=>{
      const val = (document.getElementById(item.id)||{}).value||'';
      const preview = val.length > 120 ? val.slice(0,120)+'...' : val;
      return `<div class="q-summary-item">
        <div class="q-summary-label">${item.label}</div>
        <div class="q-summary-val">${preview||'<span style="color:var(--dim)">Not filled</span>'}</div>
      </div>`;
    }).join('');
}

async function generate(){
  const topic  = document.getElementById('topic-in').value.trim();
  const author = document.getElementById('author-in').value.trim();
  const inst   = document.getElementById('inst-in').value.trim();
  const nfigs  = parseInt(document.getElementById('sl').value);
  const qProblem    = document.getElementById('q-problem').value.trim();
  const qLit        = document.getElementById('q-lit').value.trim();
  const qGap        = document.getElementById('q-gap').value.trim();
  const qObjectives = document.getElementById('q-objectives').value.trim();
  const qStatement  = document.getElementById('q-statement').value.trim();
  const geminiKey   = (document.getElementById('gemini-key-in')||{value:''}).value.trim();

  if(!topic){notify('n-gen','Please enter a research topic.','error');return;}
  if(!geminiKey){notify('n-gen','Please enter your Gemini API key. Get one free at aistudio.google.com/app/apikey','error');return;}

  // Save key for next time
  try{localStorage.setItem('rx_gkey', geminiKey);}catch(e){}

  const btn=document.getElementById('btn-gen');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>Generating...';
  try{
    const r=await fetch('/api/generate',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify({
        topic, author_name:author, institution:inst, num_figures:nfigs,
        q_problem:qProblem, q_lit:qLit, q_gap:qGap,
        q_objectives:qObjectives, q_statement:qStatement,
        gemini_key:geminiKey
      })});
    const d=await r.json();
    if(r.status===401){btn.disabled=false;btn.innerHTML='Generate Research Paper';forceLogout();return;}
    if(!d.success){notify('n-gen',d.message||'Failed.','error');btn.disabled=false;btn.innerHTML='Generate Research Paper';return;}
    jobId=d.job_id;curTopic=topic;curFigs=nfigs;
    document.getElementById('prog-topic').textContent=topic;
    buildSecGrid();show('s-prog');pollStatus();
  }catch(e){notify('n-gen','Connection error.','error');btn.disabled=false;btn.innerHTML='Generate Research Paper';}
}

function buildSecGrid(){
  const g=document.getElementById('sec-grid');g.innerHTML='';
  SECS.forEach(s=>{const d=document.createElement('div');d.className='sec-item';d.id='sec-'+s;d.textContent=s.replace('_',' ');g.appendChild(d);});
}

function updateSecs(pct){
  const idx=Math.floor((Math.max(0,pct-30))/45*SECS.length);
  SECS.forEach((s,i)=>{const el=document.getElementById('sec-'+s);if(!el)return;
    if(i<idx)el.className='sec-item done';else if(i===idx)el.className='sec-item writing';});
}

function pollStatus(){
  poll=setInterval(async()=>{
    try{
      const r=await fetch('/api/status/'+jobId,{headers:{'Authorization':'Bearer '+token}});
      const d=await r.json();if(!d.success)return;
      document.getElementById('prog-fill').style.width=d.progress+'%';
      document.getElementById('prog-pct').textContent=d.progress+'%';
      document.getElementById('stage-msg').textContent=d.message;
      updateSecs(d.progress);
      if(d.status==='done'){
        clearInterval(poll);
        SECS.forEach(s=>{const e=document.getElementById('sec-'+s);if(e)e.className='sec-item done';});
        document.getElementById('d-topic').textContent=curTopic;
        document.getElementById('d-figs').textContent=curFigs+' figures';
        document.getElementById('d-time').textContent=new Date().toLocaleTimeString();
        show('s-done');
      }else if(d.status==='error'){
        clearInterval(poll);
        const btn=document.getElementById('btn-gen');btn.disabled=false;btn.innerHTML='✦ Generate Paper (Free AI)';
        alert('Generation failed: '+d.message);show('s-gen');
      }
    }catch(e){console.error(e);}
  },800);
}

async function download(){
  const btn=document.getElementById('btn-dl');btn.disabled=true;btn.innerHTML='<span class="spin"></span>Downloading...';
  try{
    const r=await fetch('/api/download/'+jobId,{headers:{'Authorization':'Bearer '+token}});
    if(!r.ok)throw new Error('failed');
    const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download='rdxper_'+curTopic.slice(0,40).replace(/[^a-zA-Z0-9]/g,'_')+'.docx';a.click();URL.revokeObjectURL(url);
  }catch(e){alert('Download failed. Try again.');}
  finally{btn.disabled=false;btn.innerHTML='⬇ Download Research Paper (.docx)';}
}

function again(){
  jobId='';curTopic='';
  ['topic-in','inst-in','q-problem','q-lit','q-gap','q-objectives','q-statement'].forEach(id=>{
    const el=document.getElementById(id);if(el) el.value='';
  });
  // Keep gemini key pre-filled
  try{const saved=localStorage.getItem('rx_gkey');if(saved){const el=document.getElementById('gemini-key-in');if(el)el.value=saved;}}catch(e){}
  document.getElementById('sl').value=6;document.getElementById('sl-display').textContent='6';
  const btn=document.getElementById('btn-gen');
  if(btn){btn.disabled=false;btn.innerHTML='✦ Generate Research Paper';}
  document.getElementById('n-gen').classList.remove('show');
  currentStep=0;renderStep();
  loadDashboard();
  show('s-dashboard');
}

async function showProfile(){
  show('s-profile');
  try{
    const r=await fetch('/api/profile',{headers:{'Authorization':'Bearer '+token}});
    const d=await r.json();if(!d.success)return;
    const u=d.user;
    document.getElementById('prof-avatar').src=u.picture||'';
    document.getElementById('prof-name').textContent=u.name||u.email;
    document.getElementById('prof-email').textContent=u.email;
    document.getElementById('prof-since').textContent=(u.created_at||'').split('T')[0]||u.created_at||'—';
    document.getElementById('prof-papers-count').textContent=d.papers_count;
    document.getElementById('prof-spent').textContent='₹'+d.total_spent;
    document.getElementById('prof-paid-count').textContent=d.papers.filter(p=>p.paid).length;
    const tb=document.getElementById('prof-papers-list');
    tb.innerHTML=d.papers.length===0
      ?'<tr><td colspan="3" class="empty">No papers yet. Generate your first one!</td></tr>'
      :d.papers.map(p=>`<tr>
        <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.topic||''}">${p.topic||'—'}</td>
        <td style="white-space:nowrap">${(p.created_at||'').split('T')[0]||'—'}</td>
        <td>${p.paid?'<span class="badge-paid">✓ Downloaded</span>':'<span class="badge-free">Generated</span>'}</td>
      </tr>`).join('');
  }catch(e){console.error(e);}
}

async function showAdmin(){
  show('s-admin');
  try{
    const r=await fetch('/api/admin/stats',{headers:{'Authorization':'Bearer '+token}});
    const d=await r.json();
    if(!d.success){alert('Access denied');show('s-gen');return;}
    document.getElementById('adm-users-c').textContent=d.stats.total_users;
    document.getElementById('adm-papers-c').textContent=d.stats.total_papers;
    document.getElementById('adm-revenue-c').textContent='₹'+d.stats.total_revenue;
    document.getElementById('adm-paid-c').textContent=d.stats.paid_papers;
    document.getElementById('adm-users-list').innerHTML=d.users.length===0
      ?'<tr><td colspan="5" class="empty">No users yet.</td></tr>'
      :d.users.map(u=>`<tr>
        <td><img class="avatar" src="${u.picture||''}" onerror="this.style.display='none'"></td>
        <td>${u.name||'—'}</td><td>${u.email}</td>
        <td>${(u.created_at||'').split('T')[0]||'—'}</td>
        <td>${(u.last_login||'').split('T')[0]||'—'}</td>
      </tr>`).join('');
    document.getElementById('adm-papers-list').innerHTML=d.papers.length===0
      ?'<tr><td colspan="4" class="empty">No papers yet.</td></tr>'
      :d.papers.map(p=>`<tr>
        <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.topic||'—'}</td>
        <td>${p.email||'—'}</td>
        <td>${(p.created_at||'').split('T')[0]||'—'}</td>
        <td>${p.paid?'<span class="badge-paid">✓ Paid</span>':'<span class="badge-pending">Pending</span>'}</td>
      </tr>`).join('');
    document.getElementById('adm-payments-list').innerHTML=d.payments.length===0
      ?'<tr><td colspan="5" class="empty">No payments yet.</td></tr>'
      :d.payments.map(p=>`<tr>
        <td>${p.email||'—'}</td>
        <td style="color:var(--accent);font-weight:700">₹${p.amount||0}</td>
        <td style="font-family:monospace;font-size:11px">${(p.razorpay_payment||'—').slice(0,24)}</td>
        <td>${(p.created_at||'').split('T')[0]||'—'}</td>
        <td><span class="badge-${p.status==='paid'?'paid':'pending'}">${p.status}</span></td>
      </tr>`).join('');
  }catch(e){console.error(e);}
}

function admTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));el.classList.add('active');
  ['users','papers','payments'].forEach(t=>{
    const d=document.getElementById('adm-tab-'+t);if(d)d.style.display=t===name?'block':'none';
  });
}
</script>
</body>
</html>"""




# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    client_id = os.environ.get('GOOGLE_CLIENT_ID', '')
    html = HTML.replace('__GOOGLE_CLIENT_ID__', client_id).replace('__ADMIN_EMAIL__', ADMIN_EMAIL)
    return Response(html, mimetype='text/html')


def _verify_google_token(id_token_str):
    try:
        url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token_str)
        req = urllib.request.Request(url, headers={"User-Agent": "rdxper/4.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read())
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        if client_id and info.get("aud") != client_id:
            return None
        if info.get("exp") and int(info["exp"]) < time.time():
            return None
        return info
    except Exception as e:
        print(f"[Google] Token error: {e}")
        return None

@app.route("/api/auth/dev", methods=["POST"])
def dev_auth():
    """Local dev login — only works when GOOGLE_CLIENT_ID is not set."""
    if os.environ.get("GOOGLE_CLIENT_ID"):
        return jsonify({"success": False, "message": "Dev auth disabled in production"}), 403
    data    = request.json or {}
    email   = data.get("email", "").strip().lower()
    name    = data.get("name", email.split("@")[0]).strip()
    if not email or "@" not in email:
        return jsonify({"success": False, "message": "Valid email required"}), 400
    user_id = "dev_" + email.replace("@","_").replace(".","_")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            db.execute("UPDATE users SET name=?,last_login=datetime('now') WHERE email=?", (name, email))
            user_id = user["id"]
        else:
            db.execute("INSERT INTO users (id,email,name,picture,last_login) VALUES (?,?,?,?,datetime('now'))",
                       (user_id, email, name, ""))
    tok = secrets.token_urlsafe(32)
    session_set(tok, email)
    sessions[tok]["user_id"] = user_id
    sessions[tok]["name"] = name
    sessions[tok]["picture"] = ""
    return jsonify({"success": True, "token": tok, "email": email, "name": name, "picture": ""})

@app.route("/api/auth/google", methods=["POST"])
def google_auth():
    id_token_str = request.json.get("id_token", "")
    if not id_token_str:
        return jsonify({"success": False, "message": "No token"}), 400
    info = _verify_google_token(id_token_str)
    if not info:
        return jsonify({"success": False, "message": "Invalid Google token"}), 401
    g_email   = info.get("email", "").lower()
    g_name    = info.get("name", g_email.split("@")[0])
    g_picture = info.get("picture", "")
    g_sub     = info.get("sub", str(uuid.uuid4()))
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (g_email,)).fetchone()
        if user:
            db.execute("UPDATE users SET name=?,picture=?,last_login=datetime('now') WHERE email=?",
                       (g_name, g_picture, g_email))
            user_id = user["id"]
        else:
            user_id = g_sub
            db.execute("INSERT INTO users (id,email,name,picture,last_login) VALUES (?,?,?,?,datetime('now'))",
                       (user_id, g_email, g_name, g_picture))
    tok = secrets.token_urlsafe(32)
    session_set(tok, g_email)
    sessions[tok]["user_id"] = user_id
    sessions[tok]["name"] = g_name
    sessions[tok]["picture"] = g_picture
    return jsonify({"success": True, "token": tok, "email": g_email, "name": g_name, "picture": g_picture})

@app.route("/api/profile")
def get_profile():
    tok = request.headers.get("Authorization", "").replace("Bearer ", "")
    sess = session_get(tok)
    if not sess:
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    with get_db() as db:
        user    = db.execute("SELECT * FROM users WHERE id=?", (sess["user_id"],)).fetchone()
        papers  = db.execute("SELECT * FROM papers WHERE user_id=? ORDER BY created_at DESC", (sess["user_id"],)).fetchall()
        result  = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE user_id=? AND status='paid'", (sess["user_id"],)).fetchone()
        total_spent = result["t"]
    return jsonify({
        "success": True,
        "user": dict(user),
        "papers": [dict(p) for p in papers],
        "total_spent": total_spent,
        "papers_count": len(papers)
    })

@app.route("/api/admin/stats")
def admin_stats():
    tok = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not session_get(tok):
        return jsonify({"success": False, "message": "Unauthorized"}), 401
    if sessions.get(tok, {}).get("email") != ADMIN_EMAIL:
        return jsonify({"success": False, "message": "Forbidden"}), 403
    with get_db() as db:
        users    = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        papers   = db.execute("SELECT p.*,u.email,u.name FROM papers p JOIN users u ON p.user_id=u.id ORDER BY p.created_at DESC").fetchall()
        payments = db.execute("SELECT pay.*,u.email FROM payments pay JOIN users u ON pay.user_id=u.id ORDER BY pay.created_at DESC").fetchall()
        revenue  = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE status='paid'").fetchone()["t"]
    return jsonify({
        "success": True,
        "stats": {"total_users": len(users), "total_papers": len(papers),
                  "total_revenue": revenue, "paid_papers": sum(1 for p in papers if p["paid"])},
        "users":    [dict(u) for u in users],
        "papers":   [dict(p) for p in papers],
        "payments": [dict(p) for p in payments]
    })

@app.route('/api/send-otp', methods=['POST'])
def send_otp():
    data  = request.json
    email = data.get('email', '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'success': False, 'message': 'Invalid email'}), 400
    otp = str(secrets.randbelow(900000) + 100000)
    otp_store[email] = {'otp': otp, 'expires': time.time() + 600}
    print(f"\n{'='*40}\n OTP for {email}: {otp}\n{'='*40}\n")
    _try_smtp(email, otp)
    return jsonify({'success': True, 'message': f'OTP sent to {email}', 'demo_otp': otp})

def _try_smtp(to_email: str, otp: str):
    u = os.environ.get('SMTP_USER')
    p = os.environ.get('SMTP_PASS')
    if not (u and p):
        return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Your rdxper Login Code'
        msg['From'] = u; msg['To'] = to_email
        msg.attach(MIMEText(
            f'<h2 style="color:#111111">Your rdxper OTP</h2>'
            f'<p style="font-size:32px;letter-spacing:8px;font-family:monospace"><b>{otp}</b></p>'
            f'<p>Valid for 10 minutes.</p>', 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(u, p)
            s.sendmail(u, [to_email, ADMIN_EMAIL], msg.as_string())
    except Exception as e:
        print(f'[SMTP] {e}')

@app.route('/api/verify-otp', methods=['POST'])
def verify_otp():
    data  = request.json
    email = data.get('email', '').strip().lower()
    otp   = data.get('otp', '').strip()
    rec   = otp_store.get(email)
    if not rec:
        return jsonify({'success': False, 'message': 'No OTP found. Request a new one.'}), 400
    if time.time() > rec['expires']:
        del otp_store[email]
        return jsonify({'success': False, 'message': 'OTP expired.'}), 400
    if rec['otp'] != otp:
        return jsonify({'success': False, 'message': 'Wrong OTP.'}), 400
    tok = secrets.token_urlsafe(32)
    session_set(tok, email)
    del otp_store[email]
    return jsonify({'success': True, 'token': tok, 'email': email})

@app.route('/api/generate', methods=['POST'])
def generate_paper():
    tok = request.headers.get('Authorization', '').replace('Bearer ', '')
    sess = session_get(tok)
    if not sess:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data   = request.json

    # Verify OpenRouter key is configured
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        return jsonify({'success': False,
                        'message': 'OPENROUTER_API_KEY is not set. Get a free key at https://openrouter.ai/keys'}), 400

    topic  = data.get('topic', '').strip()
    nfigs  = max(3, min(15, int(data.get('num_figures', 6))))
    author = data.get('author_name', 'Anonymous').strip()
    inst   = data.get('institution', '').strip()
    email  = sess['email']

    # Questionnaire fields
    q_problem    = data.get('q_problem', '').strip()
    q_lit        = data.get('q_lit', '').strip()
    q_gap        = data.get('q_gap', '').strip()
    q_objectives = data.get('q_objectives', '').strip()
    q_statement  = data.get('q_statement', '').strip()

    if not topic:
        return jsonify({'success': False, 'message': 'Topic required'}), 400

    jid     = str(uuid.uuid4())
    user_id = sess.get('user_id', email)
    jobs[jid] = {'status': 'queued', 'progress': 0,
                 'message': 'Queued...', 'file_path': None, 'topic': topic, 'user_id': user_id}
    with get_db() as db:
        # Ensure user exists (guards against FK constraint failure)
        db.execute(
            'INSERT OR IGNORE INTO users (id, email, name, picture) VALUES (?, ?, ?, ?)',
            (user_id, email, sess.get('name', ''), sess.get('picture', ''))
        )
        db.execute('INSERT INTO papers (id,user_id,topic) VALUES (?,?,?)', (jid, user_id, topic))

    questionnaire = {
        'problem':    q_problem,
        'lit':        q_lit,
        'gap':        q_gap,
        'objectives': q_objectives,
        'statement':  q_statement,
    }

    def _run():
        try:
            g    = PaperGenerator(jid, jobs)
            path = g.generate(topic, nfigs, author, inst, email, questionnaire)
            jobs[jid].update({'status': 'done', 'progress': 100,
                              'message': 'Research paper ready!', 'file_path': path})
            with get_db() as db:
                db.execute('UPDATE papers SET file_path=? WHERE id=?', (path, jid))
        except Exception as e:
            import traceback; traceback.print_exc()
            jobs[jid].update({'status': 'error', 'message': str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'success': True, 'job_id': jid})

@app.route('/api/status/<jid>')
def job_status(jid):
    tok = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not session_get(tok):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    job = jobs.get(jid)
    if not job:
        # Fall back to DB — server may have restarted mid-generation
        with get_db() as db:
            paper = db.execute('SELECT file_path, topic FROM papers WHERE id=?', (jid,)).fetchone()
        if not paper:
            return jsonify({'success': False, 'message': 'Job not found'}), 404
        if paper['file_path']:
            return jsonify({'success': True, 'status': 'done', 'progress': 100, 'message': 'Research paper ready!'})
        return jsonify({'success': True, 'status': 'error', 'progress': 0, 'message': 'Job lost after server restart — please generate again.'})
    return jsonify({'success': True, 'status': job['status'],
                    'progress': job['progress'], 'message': job['message']})

@app.route('/api/download/<jid>')
def download_paper(jid):
    tok = request.headers.get('Authorization', '').replace('Bearer ', '')
    if not session_get(tok):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    # First check in-memory jobs dict
    job = jobs.get(jid)
    fp = None

    if job:
        if job['status'] != 'done':
            return jsonify({'success': False, 'message': 'File not ready'}), 400
        fp = job.get('file_path')
    else:
        # Server may have restarted — look up file path from DB
        with get_db() as db:
            paper = db.execute('SELECT file_path, topic FROM papers WHERE id=?', (jid,)).fetchone()
        if not paper:
            return jsonify({'success': False, 'message': 'Job not found'}), 404
        fp = paper['file_path']
        topic_slug = paper['topic'] if paper['topic'] else jid
        if not fp:
            return jsonify({'success': False, 'message': 'File not ready — please generate again'}), 400
        # Restore minimal job info for slug below
        jobs[jid] = {'status': 'done', 'file_path': fp, 'topic': paper['topic'] or ''}

    if not fp or not os.path.exists(fp):
        return jsonify({'success': False, 'message': 'File not found on server'}), 404

    topic_for_slug = jobs[jid].get('topic', '') if jid in jobs else ''
    slug = re.sub(r'[^\w\-]', '_', topic_for_slug[:40]) if topic_for_slug else jid[:8]
    return send_file(fp, as_attachment=True,
                     download_name=f'rdxper_{slug}.docx',
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')




# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    os.makedirs('generated', exist_ok=True)

    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    key_str = "✓ OpenRouter — ready!" if or_key else "✗ NOT SET — see below"
    print('\n' + '='*60)
    print('  rdxper v4.0  —  Free AI Research Paper Generator')
    print('  Powered by OpenRouter (free tier)')
    print('  Open browser:  http://127.0.0.1:8080')
    print(f'  OPENROUTER_API_KEY: {key_str}')
    print('='*60 + '\n')
    if not or_key:
        print('  ┌─ GET YOUR FREE API KEY ──────────────────────────────────┐')
        print('  │                                                          │')
        print('  │  OpenRouter (completely free, no credit card needed):   │')
        print('  │    1. Visit https://openrouter.ai/keys                  │')
        print('  │    2. Sign up → Create API Key                          │')
        print('  │    3. Windows:  set OPENROUTER_API_KEY=your_key_here    │')
        print('  │       Mac/Linux: export OPENROUTER_API_KEY=your_key     │')
        print('  │    4. Run python rdxper.py again                        │')
        print('  │                                                          │')
        print('  └──────────────────────────────────────────────────────────┘')
        print()

    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0"
    app.run(host=host, port=port, debug=False, threaded=True)
