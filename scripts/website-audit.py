#!/usr/bin/env python3
"""Evidence-oriented website readiness audit. Uses Python standard library only.

Run static inspection:
  python scripts/website-audit.py
Run static + live checks/crawl:
  python scripts/website-audit.py --url https://example.com
Write machine-readable JSON:
  python scripts/website-audit.py --url https://example.com --json website-audit.json

This is a triage tool, not a substitute for browser tests or authorized account checks.
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path.cwd()
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "coverage", ".venv", "venv"}
EXTS = {".html", ".htm", ".jsx", ".tsx", ".js", ".ts", ".astro", ".vue", ".svelte", ".md"}
CHECKS = [
 ("WLS-01","SSR/prerendering"),("WLS-02","robots.txt"),("WLS-03","sitemap.xml"),
 ("WLS-04","Unique title tags"),("WLS-05","Meta descriptions"),("WLS-06","Primary H1"),
 ("WLS-07","Canonical tags"),("WLS-08","Open Graph metadata"),("WLS-09","404 handling"),
 ("WLS-10","301 redirects"),("WLS-11","Image alt text"),("WLS-12","Image optimization"),
 ("WLS-13","Lazy loading"),("WLS-14","Organization schema"),("WLS-15","Breadcrumbs"),
 ("WLS-16","Internal links"),("WLS-17","Google Analytics"),("WLS-18","Search Console"),
 ("WLS-19","Privacy policy"),("WLS-20","Contact form"),
]
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.title=False; self.title_text=""; self.h1=0
        self.meta={}; self.canonical=[]; self.images=[]; self.links=[]; self.scripts=[]
        self.text_parts=[]; self.jsonld=False; self.jsonld_text=""; self.forms=0
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=="title": self.title=True
        if tag=="h1": self.h1+=1
        if tag=="meta":
            key=(a.get("name") or a.get("property") or "").lower()
            if key: self.meta[key]=a.get("content","")
        if tag=="link" and "canonical" in a.get("rel","").lower(): self.canonical.append(a.get("href",""))
        if tag=="img": self.images.append(a)
        if tag=="a" and a.get("href"): self.links.append(a["href"])
        if tag=="script":
            self.scripts.append(a)
            if a.get("type")=="application/ld+json": self.jsonld=True; self.jsonld_text=""
        if tag=="form": self.forms+=1
    def handle_endtag(self, tag):
        if tag=="title": self.title=False
        if tag=="script" and self.jsonld: self.jsonld=False
    def handle_data(self, data):
        if self.title: self.title_text += data
        self.text_parts.append(data)

def files_to_scan():
    for p in ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in EXTS and not any(x in SKIP_DIRS for x in p.parts):
            try:
                if p.stat().st_size < 1_500_000: yield p, p.read_text(encoding="utf-8", errors="ignore")
            except OSError: pass

def run_live(url, max_pages=30):
    origin=urlparse(url); seen=set(); q=deque([url]); pages=[]; statuses={}; robots=None; sitemap=None
    while q and len(seen)<max_pages:
        u=q.popleft()
        if u in seen: continue
        seen.add(u)
        try:
            req=Request(u,headers={"User-Agent":"WebsiteLaunchAudit/1.0 (+read-only audit)"})
            with urlopen(req,timeout=12) as r:
                ctype=r.headers.get("Content-Type","")
                body=r.read(2_000_000).decode("utf-8","replace") if "text/html" in ctype or "xml" in ctype or "text/plain" in ctype else ""
                statuses[u]={"status":r.status,"final_url":r.geturl(),"content_type":ctype}
            if u.rstrip("/")==url.rstrip("/"):
                pages.append((u,body))
                robots_url=urljoin(url,"/robots.txt"); sitemap_url=urljoin(url,"/sitemap.xml")
                for label, target in (("robots",robots_url),("sitemap",sitemap_url)):
                    try:
                        with urlopen(Request(target,headers={"User-Agent":"WebsiteLaunchAudit/1.0"}),timeout=8) as rr:
                            val=rr.read(500_000).decode("utf-8","replace")
                            if label=="robots": robots=(rr.status,val)
                            else: sitemap=(rr.status,val)
                    except Exception as e:
                        if label=="robots": robots=("ERROR",str(e))
                        else: sitemap=("ERROR",str(e))
            if "text/html" in ctype:
                pages.append((u,body))
                parser=PageParser(); parser.feed(body)
                for href in parser.links:
                    v=urljoin(u,href); parsed=urlparse(v)
                    if parsed.scheme in ("http","https") and parsed.netloc==origin.netloc and v not in seen and not parsed.fragment:
                        if parsed.path.lower().endswith((".jpg",".jpeg",".png",".gif",".webp",".avif",".pdf",".svg",".zip")): continue
                        q.append(v)
        except HTTPError as e: statuses[u]={"status":e.code,"error":str(e)}
        except Exception as e: statuses[u]={"status":"ERROR","error":str(e)}
    return pages,statuses,robots,sitemap

def main():
    ap=argparse.ArgumentParser(description="Audit a website against WLS-01..WLS-20")
    ap.add_argument("--url", help="Optional production/staging URL; performs read-only crawl")
    ap.add_argument("--json", dest="json_path", help="Write report JSON to this path")
    ap.add_argument("--max-pages", type=int, default=30, help="Maximum same-host pages to inspect (default: 30)")
    args=ap.parse_args()
    corpus=list(files_to_scan()); joined="\n".join(t for _,t in corpus).lower()
    findings={k:{"id":k,"check":name,"status":"BLOCKED","evidence":"Not verified","risk":"Review required","fix":"Inspect and verify"} for k,name in CHECKS}
    def setf(i,status,evidence,fix=""):
        f=findings[i]; f["status"]=status; f["evidence"]=evidence
        if fix: f["fix"]=fix
    # Static checks are intentionally conservative: presence is evidence, not proof of live behavior.
    for i,pat,desc in [
      ("WLS-02",r"robots\.txt","robots.txt reference"),
      ("WLS-03",r"sitemap\.xml","sitemap.xml reference"),
      ("WLS-07",r"rel=[\"']canonical|canonical","canonical implementation/reference"),
      ("WLS-08",r"og:title|og:image|property=[\"']og:","Open Graph implementation/reference"),
      ("WLS-14",r"application/ld\+json|schema\.org|\"@type\"\s*:\s*\"organization","structured data implementation/reference"),
      ("WLS-17",r"google-analytics\.com|googletagmanager\.com|gtag\(|google tag|G-[A-Z0-9]{6,}","GA/GTM implementation/reference"),
      ("WLS-18",r"google-site-verification|search console","Search Console verification reference"),
      ("WLS-19",r"privacy policy|privacy-policy|privacypolicy","privacy policy reference"),
      ("WLS-20",r"<form|useform|handleSubmit|onSubmit|contact form","form implementation/reference"),
      ("WLS-13",r"loading=[\"']lazy|loading:\s*[\"']lazy","lazy-loading implementation/reference"),
    ]:
        if re.search(pat,joined,re.I): setf(i,"PARTIAL",f"Found {desc} in {len(corpus)} scanned source files; live behavior is not proven.","Verify on rendered pages and test the live behavior.")
    if re.search(r"<h1\b|<h1>|<h1\s",joined): setf("WLS-06","PARTIAL","H1 markup appears in source; uniqueness per rendered route is not proven.","Check each indexable route for one clear primary H1.")
    if re.search(r"<title\b|<title>|\btitle:\s*['\"]",joined): setf("WLS-04","PARTIAL","Title metadata patterns found in source; per-route uniqueness is not proven.","Compare titles across all indexable routes.")
    if re.search(r"name=['\"]description|name:\s*['\"]description",joined): setf("WLS-05","PARTIAL","Meta description patterns found in source; coverage/uniqueness is not proven.","Verify description coverage and duplication across routes.")
    if re.search(r"alt=|alt:",joined): setf("WLS-11","PARTIAL","Alt attributes appear in source; image-by-image quality is not proven.","Review meaningful and decorative images on rendered pages.")
    if re.search(r"\.webp|\.avif|image/webp|image/avif",joined): setf("WLS-12","PARTIAL","Modern image format references found; delivered formats and optimization are not proven.","Inspect actual network responses and image sizes.")
    if re.search(r"breadcrumb|BreadcrumbList",joined): setf("WLS-15","PARTIAL","Breadcrumb implementation/reference found; page coverage is not proven.","Verify visible breadcrumbs and structured data on nested routes.")
    if re.search(r"notfound|not-found|404|page-not-found",joined): setf("WLS-09","PARTIAL","404-related source pattern found; HTTP status is not proven.","Request a nonexistent URL and confirm HTTP 404.")
    if re.search(r"redirect|301|permanentRedirect",joined): setf("WLS-10","PARTIAL","Redirect-related source pattern found; redirect behavior is not proven.","Test required redirects, chains, and loops.")
    if re.search(r"href=|<a\b|<link\b",joined): setf("WLS-16","PARTIAL","Link markup appears in source; destination validity/orphan pages are not proven.","Run link crawling and verify key destinations.")
    if re.search(r"renderToString|renderToPipeableStream|getServerSideProps|prerender|astro",joined): setf("WLS-01","PARTIAL","SSR/prerender-related framework or code patterns found; delivered HTML is not proven.","Inspect initial HTML for representative public routes.")
    if args.url:
        pages,statuses,robots,sitemap=run_live(args.url,args.max_pages)
        html_pages=[]
        for u,body in pages:
            p=PageParser(); p.feed(body); html_pages.append((u,p))
        if robots and robots[0]==200: setf("WLS-02","PASS",f"GET {urljoin(args.url,'/robots.txt')} returned 200.")
        elif robots: setf("WLS-02","FAIL",f"robots.txt request result: {robots[0]} {str(robots[1])[:180]}")
        if sitemap and sitemap[0]==200 and "<urlset" in sitemap[1].lower() or sitemap and sitemap[0]==200 and "<sitemapindex" in sitemap[1].lower():
            setf("WLS-03","PASS",f"GET {urljoin(args.url,'/sitemap.xml')} returned 200 and XML sitemap root was detected.")
        elif sitemap: setf("WLS-03","FAIL",f"Sitemap request result/root check: {sitemap[0]} {str(sitemap[1])[:180]}")
        if html_pages:
            titles=[(u,p.title_text.strip()) for u,p in html_pages]
            missing=[u for u,t in titles if not t]; dup=[t for t,n in Counter(t for _,t in titles if t).items() if n>1]
            setf("WLS-04","FAIL" if missing or dup else "PASS",f"Inspected {len(titles)} rendered HTML pages; missing titles={missing[:5]}, duplicate titles={dup[:5]}.")
            bad_h1=[(u,p.h1) for u,p in html_pages if p.h1!=1]
            setf("WLS-06","FAIL" if bad_h1 else "PASS",f"Inspected {len(html_pages)} pages; pages without exactly one H1={bad_h1[:8]}.")
            missing_desc=[u for u,p in html_pages if not p.meta.get("description","").strip()]
            setf("WLS-05","FAIL" if missing_desc else "PASS",f"Pages missing meta descriptions: {missing_desc[:8]}.")
            missing_can=[u for u,p in html_pages if not p.canonical or not p.canonical[0].startswith("http")]
            setf("WLS-07","FAIL" if missing_can else "PASS",f"Pages missing absolute canonical links: {missing_can[:8]}.")
            missing_og=[u for u,p in html_pages if not all(p.meta.get(x) for x in ("og:title","og:description","og:image","og:url"))]
            setf("WLS-08","FAIL" if missing_og else "PASS",f"Pages missing one or more core OG tags: {missing_og[:8]}.")
            missing_alt=[]
            for u,p in html_pages:
                if any(not im.get("alt","").strip() and im.get("role")!="presentation" for im in p.images): missing_alt.append(u)
            setf("WLS-11","FAIL" if missing_alt else "PASS",f"Pages with img elements missing/non-empty alt (review decorative exceptions): {missing_alt[:8]}.")
            no_lazy=[u for u,p in html_pages if any(not im.get("loading") for im in p.images)]
            setf("WLS-13","PARTIAL" if no_lazy else "PASS",f"Pages with images lacking loading attribute: {no_lazy[:8]}. Hero/LCP images require manual review.")
            if any("application/ld+json"==s.get("type") for _,p in html_pages for s in p.scripts):
                setf("WLS-14","PARTIAL","JSON-LD script detected in rendered HTML; parse and validate schema separately.")
            if any(p.forms for _,p in html_pages): setf("WLS-20","PARTIAL","Form element detected in rendered HTML; delivery and spam protection require end-to-end test.")
            else: setf("WLS-20","PARTIAL","No form element found in crawled rendered pages; contact mechanism may be external or JavaScript-rendered.")
            # Homepage response and crawl status evidence
            homepage=next((v for k,v in statuses.items() if k.rstrip("/")==args.url.rstrip("/")),None)
            if homepage and homepage.get("status")==200:
                setf("WLS-01","PARTIAL",f"Homepage returned 200 and {len(html_pages)} HTML pages were crawled; initial-HTML completeness requires browser/rendering review.")
            if any(v.get("status")==404 for v in statuses.values()): setf("WLS-09","PARTIAL","A crawled URL returned 404; explicitly request a guaranteed nonexistent path to validate custom 404.")
            if any(v.get("status")=="ERROR" for v in statuses.values()): setf("WLS-16","PARTIAL","Some URLs could not be fetched; inspect crawl errors in JSON output.")
        findings["live_crawl"]={"pages_inspected":len(html_pages),"responses":statuses}
    report={"target":args.url or str(ROOT),"standard":"WLS v1.0","summary":{},"checks":[findings[k] for k,_ in CHECKS],"notes":["Static pattern matches are PARTIAL, never proof of live correctness.","Analytics event delivery, Search Console access, legal review, redirects, 404 behavior, and contact delivery need dedicated verification."]}
    counts=Counter(x["status"] for x in report["checks"]); report["summary"]={k:counts.get(k,0) for k in ["PASS","FAIL","PARTIAL","BLOCKED","N/A"]}
    report["recommendation"]="NOT READY TO LAUNCH" if counts["FAIL"] else ("READY WITH ACCEPTED EXCEPTIONS" if counts["BLOCKED"] or counts["PARTIAL"] else "READY TO LAUNCH")
    print(f"Website Launch Audit — {report['target']}")
    print("Summary: "+" | ".join(f"{k}: {v}" for k,v in report["summary"].items()))
    for c in report["checks"]:
        print(f"[{c['status']:<7}] {c['id']} {c['check']} — {c['evidence']}")
    print(f"Recommendation: {report['recommendation']}")
    if args.json_path: Path(args.json_path).write_text(json.dumps(report,indent=2),encoding="utf-8"); print(f"JSON report: {args.json_path}")
    # Fail CI only for confirmed failures; blocked/partial are visible and must be reviewed.
    return 1 if counts["FAIL"] else 0
if __name__=="__main__": sys.exit(main())
