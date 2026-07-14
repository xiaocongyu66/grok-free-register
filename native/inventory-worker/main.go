// inventory-worker — Go account inventory + protocol convert for grok-free-register.
//
// Replaces the former Rust inventory-worker. Modes:
//
//	inventory-worker scan     --keys-dir keys [--json]
//	inventory-worker rebuild  --keys-dir keys
//	inventory-worker check    --keys-dir keys
//	inventory-worker convert  --keys-dir keys --formats cpa,sub2api [--pending] [--enroll] [--limit N]
//	inventory-worker version
//
// Convert paths:
//  1) OAuth file transform (CPA ↔ sub2api) — pure local, no network
//  2) SSO → OAuth via protocol (device_code + Cookie: sso=...) when --enroll
//  3) Pure register is owned by register-worker; this binary owns inventory + convert
package main

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	http "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

const (
	version       = "0.3.0-go"
	engineName    = "go"
	clientID      = "b1a00492-073a-47ea-816f-4c329264a828"
	// Scope matches grok2api SSO→Build convert + ZhuCe vault_oauth.
	scopeDefault  = "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"
	tokenEndpoint = "https://auth.x.ai/oauth2/token"
	deviceCodeURL = "https://auth.x.ai/oauth2/device/code"
	deviceVerify  = "https://auth.x.ai/oauth2/device/verify"
	deviceApprove = "https://auth.x.ai/oauth2/device/approve"
	accountsHome  = "https://accounts.x.ai/"
	apiBase       = "https://api.x.ai/v1"
	cliBase       = "https://cli-chat-proxy.grok.com/v1"
	userAgent     = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
	maxAuthBody   = 2 << 20
	// High concurrency for protocol enroll (I/O bound, Chrome TLS per worker).
	defaultConvertWorkers = 16
	maxConvertWorkers     = 64
)

type AccountRecord struct {
	ID               string            `json:"id"`
	Email            string            `json:"email"`
	Status           string            `json:"status"`
	Formats          []string          `json:"formats"`
	HasSSO           bool              `json:"has_sso"`
	HasAccessToken   bool              `json:"has_access_token"`
	HasRefreshToken  bool              `json:"has_refresh_token"`
	Subject          string            `json:"subject"`
	Fingerprint      string            `json:"fingerprint"`
	CreatedAt        string            `json:"created_at"`
	UpdatedAt        string            `json:"updated_at"`
	Paths            map[string]string `json:"paths"`
	LedgerState      string            `json:"ledger_state"`
	Source           string            `json:"source"`
	SSO              string            `json:"sso,omitempty"`
	Password         string            `json:"password,omitempty"`
}

type ScanSummary struct {
	Total       int               `json:"total"`
	ByStatus    map[string]int    `json:"by_status"`
	ByFormat    map[string]int    `json:"by_format"`
	Artifacts   map[string]bool   `json:"artifacts"`
	ExportDir   string            `json:"export_dir"`
	GeneratedAt string            `json:"generated_at"`
	Engine      string            `json:"engine"`
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	args := os.Args[2:]
	switch cmd {
	case "version", "--version", "-V":
		fmt.Printf("inventory-worker %s\n", version)
	case "check":
		os.Exit(cmdCheck(args))
	case "scan":
		os.Exit(cmdScan(args))
	case "rebuild":
		os.Exit(cmdRebuild(args))
	case "convert":
		os.Exit(cmdConvert(args))
	case "help", "--help", "-h":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", cmd)
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage: inventory-worker <scan|rebuild|check|convert|version> [flags]

  scan     --keys-dir DIR [--json]
  rebuild  --keys-dir DIR
  check    --keys-dir DIR
  convert  --keys-dir DIR --formats cpa,sub2api [--pending] [--enroll] [--limit N] [--workers N]
           [--proxy URL] [--proxy-file F] [--email E] [--emails-file F] [--sso-file F] [--progress]
  version`)
}

func parseKeysDir(args []string) (string, []string) {
	keys := strings.TrimSpace(os.Getenv("KEY_EXPORT_DIR"))
	if keys == "" {
		keys = "keys"
	}
	rest := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--keys-dir" || a == "-d" {
			if i+1 < len(args) {
				keys = args[i+1]
				i++
			}
			continue
		}
		if strings.HasPrefix(a, "--keys-dir=") {
			keys = strings.TrimPrefix(a, "--keys-dir=")
			continue
		}
		rest = append(rest, a)
	}
	if !filepath.IsAbs(keys) {
		cwd, _ := os.Getwd()
		keys = filepath.Join(cwd, keys)
	}
	return keys, rest
}

func cmdCheck(args []string) int {
	keys, _ := parseKeysDir(args)
	if err := os.MkdirAll(keys, 0o700); err != nil {
		fmt.Fprintf(os.Stderr, "[inventory-worker] cannot create %s: %v\n", keys, err)
		return 1
	}
	writeJSON(map[string]any{
		"ok":       true,
		"engine":   engineName,
		"keys_dir": keys,
		"version":  version,
	})
	return 0
}

func cmdScan(args []string) int {
	keys, rest := parseKeysDir(args)
	asJSON := false
	for _, a := range rest {
		if a == "--json" || a == "-j" {
			asJSON = true
		}
	}
	records := scanAccounts(keys)
	summary := inventorySummary(keys, records)
	if asJSON {
		// strip secrets from public scan JSON
		public := make([]AccountRecord, len(records))
		for i, r := range records {
			r.SSO = ""
			r.Password = ""
			public[i] = r
		}
		writeJSON(map[string]any{
			"ok":       true,
			"engine":   engineName,
			"summary":  summary,
			"accounts": public,
		})
		return 0
	}
	ready := summary.ByStatus["oauth_ready"]
	pending := summary.ByStatus["oauth_pending"]
	fmt.Printf("engine=%s total=%d ready=%d pending=%d dir=%s\n",
		engineName, summary.Total, ready, pending, summary.ExportDir)
	return 0
}

func cmdRebuild(args []string) int {
	keys, _ := parseKeysDir(args)
	paths, err := rebuildAll(keys)
	if err != nil {
		fmt.Fprintf(os.Stderr, "[inventory-worker] rebuild failed: %v\n", err)
		return 1
	}
	writeJSON(map[string]any{
		"ok":     true,
		"engine": engineName,
		"paths":  paths,
	})
	return 0
}

func cmdConvert(args []string) int {
	keys, rest := parseKeysDir(args)
	fs := flag.NewFlagSet("convert", flag.ContinueOnError)
	formats := fs.String("formats", "cpa,sub2api", "comma list: cpa,sub2api")
	pendingOnly := fs.Bool("pending", false, "only oauth_pending accounts")
	enroll := fs.Bool("enroll", false, "allow SSO protocol enroll (network)")
	limit := fs.Int("limit", 500, "max accounts")
	workers := fs.Int("workers", defaultConvertWorkers, "protocol enroll concurrency (parallel)")
	proxy := fs.String("proxy", "", "single http/socks proxy (or first entry of pool)")
	proxyFile := fs.String("proxy-file", "", "proxy pool file, one URL per line (round-robin per account)")
	emailOne := fs.String("email", "", "convert single email")
	emailsFile := fs.String("emails-file", "", "only these emails (one per line)")
	ssoFile := fs.String("sso-file", "", "batch SSO file: email:password:sso or email:sso per line")
	progress := fs.Bool("progress", false, "emit PROGRESS\\tjson lines on stderr")
	// Extra full attempts after first fail; re-queued at end of queue (default 1 = try twice total).
	retryMax := fs.Int("retry", 1, "extra retries for failed enroll (re-queue at end; 0=no requeue)")
	retryDelayMS := fs.Int("retry-delay-ms", 1500, "delay before a re-queued account is tried again")
	_ = fs.Parse(rest)

	// Multi-IP: proxy pool (file / env / single --proxy). Each enroll picks next via round-robin.
	proxyPool := loadProxyPool(*proxy, *proxyFile)
	var proxySeq uint64
	pickProxy := func() string {
		n := len(proxyPool)
		if n == 0 {
			return ""
		}
		if n == 1 {
			return proxyPool[0]
		}
		i := atomic.AddUint64(&proxySeq, 1) - 1
		return proxyPool[i%uint64(n)]
	}

	want := map[string]bool{}
	for _, p := range strings.Split(*formats, ",") {
		p = strings.TrimSpace(strings.ToLower(p))
		if p == "cpa" || p == "sub2api" {
			want[p] = true
		}
	}
	if len(want) == 0 {
		fmt.Fprintln(os.Stderr, "formats must include cpa and/or sub2api")
		return 2
	}

	salt := loadOrCreateSalt(keys)
	cpaIdx := indexCPA(keys)
	subIdx := indexSub2API(keys)
	legacy := loadLegacyAccounts(keys)
	sessions := loadAuthSessions(keys)

	// Default SSO source: keys/sso.txt (email:sso). Explicit --sso-file overrides path.
	if *ssoFile == "" {
		def := filepath.Join(keys, "sso.txt")
		if fileExists(def) {
			*ssoFile = def
		}
	}
	// Optional batch SSO file overrides / injects tokens (newest-last order).
	if *ssoFile != "" {
		batch := loadSSOBatchFile(*ssoFile)
		for em, acc := range batch {
			legacy[em] = acc
		}
	}
	emailFilter := map[string]bool{}
	if *emailsFile != "" {
		for _, em := range loadEmailListFile(*emailsFile) {
			emailFilter[strings.ToLower(em)] = true
		}
	}
	if *emailOne != "" {
		emailFilter[strings.ToLower(strings.TrimSpace(*emailOne))] = true
	}

	records := scanAccounts(keys)
	// Newest SSO first (accounts.txt last line = latest register).
	newestRank := newestLegacyRank(keys)
	// SSO file lines also get rank (later line = higher).
	if *ssoFile != "" {
		for em, rank := range ssoFileRank(*ssoFile) {
			if rank > newestRank[em] {
				newestRank[em] = rank
			}
		}
	}

	var targets []AccountRecord
	seen := map[string]bool{}
	// If pure sso-file batch: build targets from file first so missing inventory still works.
	if *ssoFile != "" {
		for _, acc := range loadSSOBatchOrdered(*ssoFile) {
			em := strings.ToLower(acc.Email)
			if len(emailFilter) > 0 && !emailFilter[em] {
				continue
			}
			if seen[em] {
				continue
			}
			// skip if already has CPA when pending-only
			if *pendingOnly {
				if _, ok := cpaIdx[em]; ok {
					continue
				}
			}
			seen[em] = true
			targets = append(targets, AccountRecord{
				ID:     acc.Email,
				Email:  acc.Email,
				Status: "oauth_pending",
				SSO:    acc.SSO,
				HasSSO: acc.SSO != "",
			})
		}
	}
	for _, r := range records {
		em := strings.ToLower(r.Email)
		if len(emailFilter) > 0 && !emailFilter[em] {
			continue
		}
		if seen[em] {
			// enrich SSO if missing
			if targetsSSOEmpty(targets, em) {
				if leg, ok := legacy[em]; ok && leg.SSO != "" {
					setTargetSSO(targets, em, leg.SSO)
				}
			}
			continue
		}
		if *pendingOnly && r.Status != "oauth_pending" && r.Status != "legacy_sso" {
			continue
		}
		// inject SSO from legacy/batch if scan missed it
		if r.SSO == "" {
			if leg, ok := legacy[em]; ok {
				r.SSO = leg.SSO
				r.HasSSO = r.SSO != ""
			}
		}
		seen[em] = true
		targets = append(targets, r)
	}
	sort.SliceStable(targets, func(i, j int) bool {
		ri, rj := newestRank[strings.ToLower(targets[i].Email)], newestRank[strings.ToLower(targets[j].Email)]
		if ri != rj {
			return ri > rj // higher rank = newer
		}
		return targets[i].Email < targets[j].Email
	})
	if *limit > 0 && len(targets) > *limit {
		targets = targets[:*limit]
	}

	results := make([]convertResult, 0, len(targets))
	var mu sync.Mutex
	okN, failN, skipN := 0, 0, 0
	doneN := 0
	totalJobs := len(targets)
	emitProgress := func(email string, oneOK bool, errMsg string) {
		if !*progress {
			return
		}
		mu.Lock()
		doneN++
		payload := map[string]any{
			"event":  "progress",
			"done":   doneN,
			"total":  totalJobs,
			"ok":     okN,
			"fail":   failN,
			"skip":   skipN,
			"email":  email,
			"ok_one": oneOK,
		}
		if errMsg != "" {
			payload["error"] = truncate(errMsg, 160)
		}
		mu.Unlock()
		b, _ := json.Marshal(payload)
		fmt.Fprintf(os.Stderr, "PROGRESS\t%s\n", b)
	}
	if *progress {
		b, _ := json.Marshal(map[string]any{
			"event": "start", "total": totalJobs, "workers": *workers, "enroll": *enroll,
		})
		fmt.Fprintf(os.Stderr, "PROGRESS\t%s\n", b)
	}

	// Phase 1: pure OAuth transform (serial, fast)
	var needEnroll []AccountRecord
	for _, r := range targets {
		res := convertOAuthCopy(keys, r.Email, want, cpaIdx, subIdx, salt, true, false)
		if res.OK {
			mu.Lock()
			results = append(results, res)
			okN++
			mu.Unlock()
			emitProgress(r.Email, true, "")
			continue
		}
		if res.NeedSSO && *enroll {
			needEnroll = append(needEnroll, r)
			continue
		}
		mu.Lock()
		results = append(results, res)
		if res.Error != "" {
			failN++
		} else {
			skipN++
		}
		mu.Unlock()
		emitProgress(r.Email, false, res.Error)
	}

	// Phase 2: protocol SSO enroll — worker pool + re-queue failed jobs at end.
	if *enroll && len(needEnroll) > 0 {
		w := *workers
		if w < 1 {
			w = defaultConvertWorkers
		}
		if w > maxConvertWorkers {
			w = maxConvertWorkers
		}
		if w > len(needEnroll) {
			w = len(needEnroll)
		}
		extraRetry := *retryMax
		if extraRetry < 0 {
			extraRetry = 0
		}
		if extraRetry > 5 {
			extraRetry = 5
		}
		delay := time.Duration(*retryDelayMS) * time.Millisecond
		if delay < 0 {
			delay = 0
		}
		fmt.Fprintf(os.Stderr,
			"[inventory-worker] protocol enroll workers=%d jobs=%d tls=chrome_131 proxies=%d retry=%d delay=%s\n",
			w, len(needEnroll), len(proxyPool), extraRetry, delay)
		if *progress {
			b, _ := json.Marshal(map[string]any{
				"event": "enroll_start", "jobs": len(needEnroll), "workers": w,
				"proxies": len(proxyPool), "retry": extraRetry,
			})
			fmt.Fprintf(os.Stderr, "PROGRESS\t%s\n", b)
		}

		type enrollJob struct {
			rec     AccountRecord
			attempt int // 0 = first try
		}
		// Buffered queue: initial jobs + room for re-queues.
		qcap := len(needEnroll)*(extraRetry+1) + w + 8
		if qcap < 64 {
			qcap = 64
		}
		jobs := make(chan enrollJob, qcap)
		var pending atomic.Int64
		pending.Store(int64(len(needEnroll)))
		for _, r := range needEnroll {
			jobs <- enrollJob{rec: r, attempt: 0}
		}

		var wg sync.WaitGroup
		for i := 0; i < w; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for {
					job, ok := <-jobs
					if !ok {
						return
					}
					r := job.rec
					email := r.Email
					sso := r.SSO
					if sso == "" {
						if leg, ok := legacy[strings.ToLower(email)]; ok {
							sso = leg.SSO
						}
					}
					if sso == "" {
						if sess, ok := sessions[strings.ToLower(email)]; ok {
							sso = sess.SSO
						}
					}
					res := convertResult{Email: email, Method: "protocol_enroll_go"}
					if job.attempt > 0 {
						res.Method = fmt.Sprintf("protocol_enroll_go_retry%d", job.attempt)
					}

					finishFail := func(errMsg string, permanent bool) {
						// Re-queue at end if retries remain and error looks retryable.
						if !permanent && job.attempt < extraRetry && isRetryableEnrollError(errMsg) {
							if *progress {
								b, _ := json.Marshal(map[string]any{
									"event":   "retry_queued",
									"email":   email,
									"attempt": job.attempt + 1,
									"max":     extraRetry,
									"error":   truncate(errMsg, 120),
								})
								fmt.Fprintf(os.Stderr, "PROGRESS\t%s\n", b)
							}
							if delay > 0 {
								time.Sleep(delay)
							}
							// Keep pending count; push to back of queue.
							select {
							case jobs <- enrollJob{rec: r, attempt: job.attempt + 1}:
							default:
								// queue full — fall through to final fail
								goto finalFail
							}
							return
						}
					finalFail:
						res.Error = errMsg
						mu.Lock()
						results = append(results, res)
						failN++
						mu.Unlock()
						emitProgress(email, false, errMsg)
						if pending.Add(-1) == 0 {
							close(jobs)
						}
					}

					if sso == "" {
						finishFail("no SSO session", true)
						continue
					}
					px := pickProxy()
					doc, err := protocolSSOToOAuth(context.Background(), email, sso, px)
					if err != nil {
						// immediate alternate proxy attempt (same attempt slot)
						if len(proxyPool) > 1 {
							px2 := pickProxy()
							if px2 != "" && px2 != px {
								if doc2, err2 := protocolSSOToOAuth(context.Background(), email, sso, px2); err2 == nil {
									doc, err = doc2, nil
								} else {
									err = err2
								}
							}
						}
					}
					if err != nil {
						finishFail(err.Error(), false)
						continue
					}
					written := []string{}
					if want["cpa"] {
						p, errW := writeCPA(keys, email, doc, salt, true)
						if errW == nil {
							written = append(written, "cpa:"+filepath.Base(p))
							mu.Lock()
							cpaIdx[strings.ToLower(email)] = doc
							mu.Unlock()
						}
					}
					if want["sub2api"] {
						item := sub2apiFromCPA(doc)
						p, errW := writeSub2API(keys, email, item, salt, false)
						if errW == nil {
							written = append(written, "sub2api:"+filepath.Base(p))
							mu.Lock()
							subIdx[strings.ToLower(email)] = item
							mu.Unlock()
						}
					}
					res.OK = len(written) > 0
					res.Written = written
					if !res.OK {
						finishFail("write failed", false)
						continue
					}
					mu.Lock()
					results = append(results, res)
					okN++
					mu.Unlock()
					emitProgress(email, true, "")
					if pending.Add(-1) == 0 {
						close(jobs)
					}
				}
			}()
		}
		wg.Wait()
	}

	// always rebuild sub2api bundle once
	if _, err := rebuildSub2API(keys); err != nil {
		fmt.Fprintf(os.Stderr, "[inventory-worker] bundle rebuild: %v\n", err)
	}
	_ = rebuildCPA(keys)

	if *progress {
		b, _ := json.Marshal(map[string]any{
			"event": "done", "ok": okN, "fail": failN, "skip": skipN, "total": len(targets),
		})
		fmt.Fprintf(os.Stderr, "PROGRESS\t%s\n", b)
	}

	writeJSON(map[string]any{
		"ok":      failN == 0,
		"engine":  engineName,
		"total":   len(targets),
		"ok_n":    okN,
		"fail_n":  failN,
		"skip_n":  skipN,
		"results": results,
	})
	if failN > 0 {
		return 1
	}
	return 0
}

func loadEmailListFile(path string) []string {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out []string
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// allow email:password:sso — take first field
		if i := strings.IndexByte(line, ':'); i > 0 {
			// if looks like email:pass:sso keep email only for filter lists
			if strings.Count(line, ":") >= 2 && strings.Contains(line[:i], "@") {
				line = line[:i]
			} else if strings.Contains(line, "@") && !strings.Contains(line[:i], "@") {
				// keep whole if weird
			} else if strings.Contains(line[:i], "@") {
				line = line[:i]
			}
		}
		line = strings.TrimSpace(line)
		if line != "" {
			out = append(out, line)
		}
	}
	return out
}

func loadSSOBatchFile(path string) map[string]legacyAcc {
	out := map[string]legacyAcc{}
	for _, acc := range loadSSOBatchOrdered(path) {
		out[strings.ToLower(acc.Email)] = acc
	}
	return out
}

func loadSSOBatchOrdered(path string) []legacyAcc {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out []legacyAcc
	seen := map[string]int{}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		// email:password:sso  OR  email:sso  OR  sso=...
		var email, password, sso string
		parts := strings.SplitN(line, ":", 3)
		if len(parts) >= 3 && strings.Contains(parts[0], "@") {
			email, password, sso = strings.TrimSpace(parts[0]), parts[1], parts[2]
		} else if len(parts) == 2 && strings.Contains(parts[0], "@") {
			email, sso = strings.TrimSpace(parts[0]), parts[1]
		} else {
			continue
		}
		sso = normalizeSSOToken(sso)
		if email == "" || sso == "" {
			continue
		}
		em := strings.ToLower(email)
		if idx, ok := seen[em]; ok {
			out[idx] = legacyAcc{Email: email, Password: password, SSO: sso}
			continue
		}
		seen[em] = len(out)
		out = append(out, legacyAcc{Email: email, Password: password, SSO: sso})
	}
	return out
}

func ssoFileRank(path string) map[string]int {
	out := map[string]int{}
	data, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	// base high so file ranks sit above accounts.txt when desired
	base := 1_000_000
	for i, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || !strings.Contains(line, "@") {
			continue
		}
		parts := strings.SplitN(line, ":", 3)
		if len(parts) < 1 {
			continue
		}
		em := strings.ToLower(strings.TrimSpace(parts[0]))
		if em != "" {
			out[em] = base + i
		}
	}
	return out
}

func targetsSSOEmpty(targets []AccountRecord, em string) bool {
	for _, t := range targets {
		if strings.EqualFold(t.Email, em) {
			return t.SSO == ""
		}
	}
	return true
}

func setTargetSSO(targets []AccountRecord, em, sso string) {
	for i := range targets {
		if strings.EqualFold(targets[i].Email, em) {
			targets[i].SSO = sso
			targets[i].HasSSO = sso != ""
			return
		}
	}
}

// ---------- scan ----------

func scanAccounts(root string) []AccountRecord {
	byEmail := map[string]*AccountRecord{}

	// accounts.txt  email:password (or legacy email:password:sso)
	accountsTxt := filepath.Join(root, "accounts.txt")
	if b, err := os.ReadFile(accountsTxt); err == nil {
		mt := mtimeISO(accountsTxt)
		for _, line := range strings.Split(string(b), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			parts := strings.Split(line, ":")
			if len(parts) < 2 {
				continue
			}
			email := strings.TrimSpace(parts[0])
			if email == "" {
				continue
			}
			password := ""
			sso := ""
			if len(parts) >= 2 {
				password = strings.TrimSpace(parts[1])
			}
			if len(parts) >= 3 {
				sso = strings.TrimSpace(parts[2])
			}
			rec := ensureRec(byEmail, email)
			if !hasFmt(rec, "legacy") {
				rec.Formats = append(rec.Formats, "legacy")
			}
			rec.HasSSO = rec.HasSSO || sso != ""
			if sso != "" {
				rec.SSO = sso
			}
			if password != "" {
				rec.Password = password
			}
			if rec.Paths == nil {
				rec.Paths = map[string]string{}
			}
			rec.Paths["legacy"] = accountsTxt
			if rec.UpdatedAt == "" {
				rec.UpdatedAt = mt
			}
			if rec.Source == "" {
				rec.Source = "accounts.txt"
			}
			if sso != "" && rec.Status == "unknown" {
				rec.Status = "legacy_sso"
			} else if sso != "" && rec.Status == "" {
				rec.Status = "legacy_sso"
			}
		}
	}

	// keys/sso.txt — canonical email:sso
	ssoTxt := filepath.Join(root, "sso.txt")
	if b, err := os.ReadFile(ssoTxt); err == nil {
		mt := mtimeISO(ssoTxt)
		for _, line := range strings.Split(string(b), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			email, sso, ok := strings.Cut(line, ":")
			email = strings.TrimSpace(email)
			sso = normalizeSSOToken(sso)
			if !ok || email == "" || !strings.Contains(email, "@") || sso == "" {
				continue
			}
			rec := ensureRec(byEmail, email)
			if !hasFmt(rec, "sso") {
				rec.Formats = append(rec.Formats, "sso")
			}
			rec.HasSSO = true
			rec.SSO = sso
			if rec.Paths == nil {
				rec.Paths = map[string]string{}
			}
			rec.Paths["sso"] = ssoTxt
			if rec.UpdatedAt == "" || rec.Source == "accounts.txt" {
				rec.UpdatedAt = mt
			}
			if rec.Source == "" || rec.Source == "accounts.txt" {
				rec.Source = "sso.txt"
			}
			if rec.Status == "unknown" || rec.Status == "legacy_sso" || rec.Status == "" {
				rec.Status = "oauth_pending"
			}
		}
	}

	// also absorb auth-sessions for SSO
	for email, sess := range loadAuthSessions(root) {
		rec := ensureRec(byEmail, email)
		if sess.SSO != "" {
			rec.HasSSO = true
			if rec.SSO == "" {
				rec.SSO = sess.SSO
			}
			if !hasFmt(rec, "legacy") && rec.Status == "unknown" {
				rec.Status = "legacy_sso"
			}
		}
	}

	// sub2api singles
	subDir := filepath.Join(root, "sub2api")
	if ents, err := os.ReadDir(subDir); err == nil {
		var paths []string
		for _, e := range ents {
			n := e.Name()
			if strings.HasSuffix(n, ".sub2api.json") && n != "accounts.sub2api.json" {
				paths = append(paths, filepath.Join(subDir, n))
			}
		}
		sort.Strings(paths)
		for _, path := range paths {
			doc := readJSONMap(path)
			if doc == nil {
				continue
			}
			fp := strings.TrimSuffix(filepath.Base(path), ".sub2api.json")
			accounts, _ := doc["accounts"].([]any)
			for _, raw := range accounts {
				item, ok := raw.(map[string]any)
				if !ok {
					continue
				}
				creds, _ := item["credentials"].(map[string]any)
				extra, _ := item["extra"].(map[string]any)
				if creds == nil {
					creds = map[string]any{}
				}
				if extra == nil {
					extra = map[string]any{}
				}
				email := strAny(creds["email"])
				if email == "" {
					email = strAny(extra["email"])
				}
				if email == "" {
					email = strAny(item["name"])
				}
				email = strings.TrimSpace(email)
				if email == "" {
					continue
				}
				rec := ensureRec(byEmail, email)
				if !hasFmt(rec, "sub2api") {
					rec.Formats = append(rec.Formats, "sub2api")
				}
				at := strAny(creds["access_token"])
				rt := strAny(creds["refresh_token"])
				rec.HasAccessToken = rec.HasAccessToken || at != ""
				rec.HasRefreshToken = rec.HasRefreshToken || rt != ""
				if rec.Subject == "" {
					rec.Subject = firstNonEmpty(strAny(extra["subject"]), strAny(creds["sub"]))
				}
				if rec.Fingerprint == "" {
					rec.Fingerprint = fp
				}
				if rec.Paths == nil {
					rec.Paths = map[string]string{}
				}
				rec.Paths["sub2api"] = path
				mt := mtimeISO(path)
				if mt > rec.UpdatedAt {
					rec.UpdatedAt = mt
				}
				if rec.CreatedAt == "" {
					rec.CreatedAt = strAny(doc["exported_at"])
				}
				rec.Status = "oauth_ready"
			}
		}
	}

	// cpa singles
	cpaDir := filepath.Join(root, "cpa")
	_ = purgeCPAMerge(cpaDir)
	if ents, err := os.ReadDir(cpaDir); err == nil {
		var paths []string
		for _, e := range ents {
			n := e.Name()
			if strings.HasPrefix(n, "xai-") && strings.HasSuffix(n, ".json") {
				paths = append(paths, filepath.Join(cpaDir, n))
			}
		}
		sort.Strings(paths)
		for _, path := range paths {
			doc := readJSONMap(path)
			if doc == nil {
				continue
			}
			email := strings.TrimSpace(strAny(doc["email"]))
			if email == "" {
				email = strings.TrimSpace(strAny(doc["name"]))
			}
			if email == "" {
				email = strings.TrimSuffix(filepath.Base(path), ".json")
			}
			fp := strings.TrimSuffix(filepath.Base(path), ".json")
			rec := ensureRec(byEmail, email)
			if !hasFmt(rec, "cpa") {
				rec.Formats = append(rec.Formats, "cpa")
			}
			at := strAny(doc["access_token"])
			rt := strAny(doc["refresh_token"])
			rec.HasAccessToken = rec.HasAccessToken || at != ""
			rec.HasRefreshToken = rec.HasRefreshToken || rt != ""
			if rec.Subject == "" {
				rec.Subject = strAny(doc["sub"])
			}
			if rec.Fingerprint == "" {
				rec.Fingerprint = fp
			}
			if rec.Paths == nil {
				rec.Paths = map[string]string{}
			}
			rec.Paths["cpa"] = path
			mt := mtimeISO(path)
			if mt > rec.UpdatedAt {
				rec.UpdatedAt = mt
			}
			if rec.Status != "oauth_ready" && (rec.HasAccessToken || rec.HasRefreshToken) {
				rec.Status = "oauth_ready"
			}
		}
	}

	out := make([]AccountRecord, 0, len(byEmail))
	for _, rec := range byEmail {
		if rec.HasSSO && !rec.HasAccessToken && !rec.HasRefreshToken &&
			!hasFmt(rec, "sub2api") && !hasFmt(rec, "cpa") {
			rec.Status = "oauth_pending"
		}
		if len(rec.Formats) == 0 {
			rec.Formats = []string{"unknown"}
		}
		sort.Strings(rec.Formats)
		rec.Formats = uniqueStrings(rec.Formats)
		if rec.Status == "" {
			rec.Status = "unknown"
		}
		out = append(out, *rec)
	}
	order := map[string]int{"oauth_ready": 0, "oauth_pending": 1, "legacy_sso": 2}
	sort.Slice(out, func(i, j int) bool {
		oi, oj := order[out[i].Status], order[out[j].Status]
		if oi == 0 && out[i].Status != "oauth_ready" {
			oi = 9
		}
		if oj == 0 && out[j].Status != "oauth_ready" {
			oj = 9
		}
		if oi != oj {
			return oi < oj
		}
		if out[i].UpdatedAt != out[j].UpdatedAt {
			return out[i].UpdatedAt > out[j].UpdatedAt
		}
		return out[i].Email < out[j].Email
	})
	return out
}

func ensureRec(m map[string]*AccountRecord, email string) *AccountRecord {
	if r, ok := m[email]; ok {
		return r
	}
	r := &AccountRecord{
		ID:     email,
		Email:  email,
		Status: "unknown",
		Paths:  map[string]string{},
	}
	m[email] = r
	return r
}

func hasFmt(r *AccountRecord, f string) bool {
	for _, x := range r.Formats {
		if x == f {
			return true
		}
	}
	return false
}

func inventorySummary(root string, records []AccountRecord) ScanSummary {
	byStatus := map[string]int{}
	byFormat := map[string]int{}
	for _, r := range records {
		byStatus[r.Status]++
		for _, f := range r.Formats {
			byFormat[f]++
		}
	}
	cpaSingles := 0
	if ents, err := os.ReadDir(filepath.Join(root, "cpa")); err == nil {
		for _, e := range ents {
			n := e.Name()
			if strings.HasPrefix(n, "xai-") && strings.HasSuffix(n, ".json") {
				cpaSingles++
			}
		}
	}
	return ScanSummary{
		Total:    len(records),
		ByStatus: byStatus,
		ByFormat: byFormat,
		Artifacts: map[string]bool{
			"legacy_accounts_txt": fileExists(filepath.Join(root, "accounts.txt")),
			"sub2api_bundle":      fileExists(filepath.Join(root, "sub2api", "accounts.sub2api.json")),
			"cpa_bundle_json":     false,
			"cpa_bundle_zip":      false,
			"cpa_singles":         cpaSingles > 0,
		},
		ExportDir:   root,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339),
		Engine:      engineName,
	}
}

// ---------- rebuild ----------

func rebuildAll(root string) (map[string]string, error) {
	paths := map[string]string{}
	sub, err := rebuildSub2API(root)
	if err != nil {
		return nil, err
	}
	paths["sub2api_json"] = sub
	if err := rebuildCPA(root); err != nil {
		return nil, err
	}
	cpaDir := filepath.Join(root, "cpa")
	if ents, err := os.ReadDir(cpaDir); err == nil {
		count := 0
		for _, e := range ents {
			n := e.Name()
			if strings.HasPrefix(n, "xai-") && strings.HasSuffix(n, ".json") {
				count++
			}
		}
		if count > 0 {
			paths["cpa_dir"] = cpaDir
			paths["cpa_singles"] = strconv.Itoa(count)
		}
	}
	legacy := filepath.Join(root, "accounts.txt")
	if fileExists(legacy) {
		paths["legacy_txt"] = legacy
	}
	return paths, nil
}

func rebuildSub2API(root string) (string, error) {
	directory := filepath.Join(root, "sub2api")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", err
	}
	var accounts []any
	seen := map[string]bool{}
	ents, _ := os.ReadDir(directory)
	var paths []string
	for _, e := range ents {
		n := e.Name()
		if strings.HasSuffix(n, ".sub2api.json") && n != "accounts.sub2api.json" {
			paths = append(paths, filepath.Join(directory, n))
		}
	}
	sort.Strings(paths)
	for _, path := range paths {
		doc := readJSONMap(path)
		if doc == nil {
			continue
		}
		items, _ := doc["accounts"].([]any)
		for _, raw := range items {
			item, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			creds, _ := item["credentials"].(map[string]any)
			if creds == nil {
				creds = map[string]any{}
			}
			key := fmt.Sprintf("%s|%s|%s|%s",
				strAny(item["platform"]),
				strAny(creds["refresh_token"]),
				strAny(creds["access_token"]),
				strAny(item["name"]),
			)
			if seen[key] {
				continue
			}
			seen[key] = true
			accounts = append(accounts, item)
		}
	}
	if accounts == nil {
		accounts = []any{}
	}
	out := map[string]any{
		"exported_at": time.Now().UTC().Format(time.RFC3339),
		"proxies":     []any{},
		"accounts":    accounts,
		"engine":      engineName,
	}
	target := filepath.Join(directory, "accounts.sub2api.json")
	if err := atomicWriteJSON(target, out); err != nil {
		return "", err
	}
	return target, nil
}

func rebuildCPA(root string) error {
	directory := filepath.Join(root, "cpa")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	return purgeCPAMerge(directory)
}

func purgeCPAMerge(directory string) error {
	for _, bad := range []string{"accounts.cpa.json", "accounts.cpa.zip", "accounts.cpa.zip.tmp"} {
		_ = os.Remove(filepath.Join(directory, bad))
	}
	return nil
}

// ---------- convert oauth copy ----------

type convertResult struct {
	OK      bool     `json:"ok"`
	Email   string   `json:"email"`
	Method  string   `json:"method,omitempty"`
	Error   string   `json:"error,omitempty"`
	Written []string `json:"written,omitempty"`
	NeedSSO bool     `json:"need_sso,omitempty"`
}

func convertOAuthCopy(
	root, email string,
	formats map[string]bool,
	cpaIdx map[string]map[string]any,
	subIdx map[string]map[string]any,
	salt []byte,
	purgeCPA bool,
	rebuildBundle bool,
) convertResult {
	res := convertResult{Email: email, Method: "oauth_copy"}

	emailL := strings.ToLower(strings.TrimSpace(email))
	cpaDoc := cpaIdx[emailL]
	subItem := subIdx[emailL]
	written := []string{}

	if formats["cpa"] {
		if cpaDoc != nil {
			written = append(written, "cpa:already")
		} else if subItem != nil {
			doc := cpaFromSub2API(subItem)
			if strAny(doc["access_token"]) == "" && strAny(doc["refresh_token"]) == "" {
				res.Error = "sub2api missing tokens"
				return res
			}
			p, err := writeCPA(root, email, doc, salt, purgeCPA)
			if err != nil {
				res.Error = err.Error()
				return res
			}
			written = append(written, "cpa:"+filepath.Base(p))
			cpaIdx[emailL] = doc
			cpaDoc = doc
		} else {
			res.Error = "no oauth source for cpa"
			res.NeedSSO = true
			return res
		}
	}

	if formats["sub2api"] {
		if subItem != nil {
			written = append(written, "sub2api:already")
		} else {
			src := cpaIdx[emailL]
			if src == nil {
				src = cpaDoc
			}
			if src != nil {
				item := sub2apiFromCPA(src)
				p, err := writeSub2API(root, email, item, salt, rebuildBundle)
				if err != nil {
					res.Error = err.Error()
					return res
				}
				written = append(written, "sub2api:"+filepath.Base(p))
				subIdx[emailL] = item
			} else {
				res.Error = "no oauth source for sub2api"
				res.NeedSSO = true
				return res
			}
		}
	}

	res.OK = true
	res.Written = written
	return res
}

func cpaFromSub2API(item map[string]any) map[string]any {
	creds, _ := item["credentials"].(map[string]any)
	extra, _ := item["extra"].(map[string]any)
	if creds == nil {
		creds = map[string]any{}
	}
	if extra == nil {
		extra = map[string]any{}
	}
	email := firstNonEmpty(strAny(creds["email"]), strAny(extra["email"]), strAny(item["name"]))
	return map[string]any{
		"type":           "xai",
		"access_token":   creds["access_token"],
		"refresh_token":  creds["refresh_token"],
		"id_token":       creds["id_token"],
		"token_type":     firstNonEmpty(strAny(creds["token_type"]), "Bearer"),
		"expires_in":     creds["expires_in"],
		"expired":        firstNonEmpty(strAny(creds["expires_at"]), strAny(creds["expired"])),
		"last_refresh":   firstNonEmpty(strAny(extra["last_refresh"]), strAny(creds["last_refresh"])),
		"sub":            firstNonEmpty(strAny(extra["subject"]), strAny(creds["sub"])),
		"base_url":       firstNonEmpty(strAny(creds["base_url"]), apiBase),
		"token_endpoint": firstNonEmpty(strAny(creds["token_endpoint"]), tokenEndpoint),
		"auth_kind":      "oauth",
		"email":          email,
	}
}

func sub2apiFromCPA(doc map[string]any) map[string]any {
	email := firstNonEmpty(strAny(doc["email"]), strAny(doc["name"]))
	credentials := map[string]any{
		"access_token":  doc["access_token"],
		"refresh_token": doc["refresh_token"],
		"expires_at":    firstNonEmpty(strAny(doc["expired"]), strAny(doc["expires_at"])),
		"client_id":     clientID,
		"scope":         scopeDefault,
		"email":         email,
		"base_url":      firstNonEmpty(strAny(doc["base_url"]), apiBase),
	}
	if v := strAny(doc["id_token"]); v != "" {
		credentials["id_token"] = v
	}
	if v := strAny(doc["token_type"]); v != "" {
		credentials["token_type"] = v
	}
	return map[string]any{
		"name":         firstNonEmpty(email, "grok-account"),
		"platform":     "grok",
		"type":         "oauth",
		"concurrency":  10,
		"priority":     1,
		"credentials":  credentials,
		"extra": map[string]any{
			"email":        email,
			"subject":      doc["sub"],
			"last_refresh": doc["last_refresh"],
		},
	}
}

func writeCPA(root, email string, doc map[string]any, salt []byte, purge bool) (string, error) {
	directory := filepath.Join(root, "cpa")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", err
	}
	if purge {
		_ = purgeCPAMerge(directory)
	}
	sub := firstNonEmpty(strAny(doc["sub"]), email)
	digest := hmacHex(salt, sub)[:16]
	path := filepath.Join(directory, "xai-"+digest+".json")
	payload := map[string]any{}
	for k, v := range doc {
		payload[k] = v
	}
	payload["email"] = email
	if err := atomicWriteJSON(path, payload); err != nil {
		return "", err
	}
	return path, nil
}

func writeSub2API(root, email string, account map[string]any, salt []byte, rebuild bool) (string, error) {
	directory := filepath.Join(root, "sub2api")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", err
	}
	extra, _ := account["extra"].(map[string]any)
	subject := email
	if extra != nil {
		if s := strAny(extra["subject"]); s != "" {
			subject = s
		}
	}
	digest := hmacHex(salt, subject)[:16]
	path := filepath.Join(directory, "xai-"+digest+".sub2api.json")
	doc := map[string]any{
		"exported_at": time.Now().UTC().Format(time.RFC3339),
		"proxies":     []any{},
		"accounts":    []any{account},
	}
	if err := atomicWriteJSON(path, doc); err != nil {
		return "", err
	}
	if rebuild {
		_, _ = rebuildSub2API(root)
	}
	return path, nil
}

// ---------- protocol SSO → OAuth (grok2api sso_build + ZhuCe vault_oauth) ----------
//
// Pure HTTP, no browser:
//   GET accounts.x.ai (warm SSO cookie)
//   POST oauth2/device/code
//   GET  verification_uri_complete
//   POST oauth2/device/verify  {user_code}
//   POST oauth2/device/approve {user_code, action=allow, ...}
//   poll oauth2/token
//
// Reference: chenyme/grok2api backend/internal/infra/provider/web/sso_build.go
//            DrognLI/Grok_ZhuCe_Xi_Yi app/auth_cli/vault_oauth.py

type ssoBuildFlow struct {
	client  tls_client.HttpClient
	cookies map[string]string
}

func newChromeTLSClient(proxy string) (tls_client.HttpClient, error) {
	// Chrome 131 JA3/JA4 fingerprint — required to avoid CF 403 on accounts.x.ai
	// (plain crypto/tls Go client is fingerprinted and blocked on this host).
	opts := []tls_client.HttpClientOption{
		tls_client.WithTimeoutSeconds(90),
		tls_client.WithClientProfile(profiles.Chrome_131),
		tls_client.WithNotFollowRedirects(), // manual redirect like grok2api
		tls_client.WithInsecureSkipVerify(),
	}
	if proxy == "" {
		proxy = firstNonEmpty(os.Getenv("HTTPS_PROXY"), os.Getenv("HTTP_PROXY"), os.Getenv("ALL_PROXY"))
	}
	if proxy != "" {
		opts = append(opts, tls_client.WithProxyUrl(proxy))
	}
	return tls_client.NewHttpClient(tls_client.NewNoopLogger(), opts...)
}

func protocolSSOToOAuth(ctx context.Context, email, sso, proxy string) (map[string]any, error) {
	sso = normalizeSSOToken(sso)
	if sso == "" {
		return nil, errors.New("empty sso token")
	}
	client, err := newChromeTLSClient(proxy)
	if err != nil {
		return nil, fmt.Errorf("tls client: %w", err)
	}
	flow := &ssoBuildFlow{
		client:  client,
		cookies: map[string]string{"sso": sso, "sso-rw": sso},
	}
	return flow.convert(ctx, email)
}

// newestLegacyRank maps email → line index in accounts.txt (higher = newer).
func newestLegacyRank(root string) map[string]int {
	out := map[string]int{}
	data, err := os.ReadFile(filepath.Join(root, "accounts.txt"))
	if err != nil {
		return out
	}
	lines := strings.Split(string(data), "\n")
	for i, line := range lines {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, ":", 3)
		if len(parts) < 1 {
			continue
		}
		em := strings.ToLower(strings.TrimSpace(parts[0]))
		if em == "" {
			continue
		}
		out[em] = i
	}
	return out
}

// isRetryableEnrollError: re-queue at end. Permanent SSO death is not retried.
func isRetryableEnrollError(msg string) bool {
	m := strings.ToLower(msg)
	if m == "" {
		return true
	}
	// permanent — cookie/session dead or missing
	permanent := []string{
		"no sso session",
		"sso unauthorized",
		"cookie expired",
		"oauth_denied",
		"access_denied",
		"empty sso",
	}
	for _, p := range permanent {
		if strings.Contains(m, p) {
			return false
		}
	}
	return true
}

// loadProxyPool builds ordered unique proxy list for multi-IP enroll.
// Sources (merged, de-duped): --proxy, --proxy-file, SSO_CONVERT_PROXY_FILE,
// PROXY_POOL_FILE / 代理.txt, PROXY_POOL env (comma/newline), HTTPS_PROXY.
func loadProxyPool(single, proxyFile string) []string {
	var raw []string
	add := func(s string) {
		s = strings.TrimSpace(s)
		if s == "" || strings.HasPrefix(s, "#") {
			return
		}
		// allow host:port without scheme → socks5h:// or http://
		if !strings.Contains(s, "://") {
			if strings.HasPrefix(s, "socks") {
				// already weird
			} else {
				// default http for host:port
				s = "http://" + s
			}
		}
		raw = append(raw, s)
	}
	if single != "" {
		for _, p := range splitProxyList(single) {
			add(p)
		}
	}
	files := []string{}
	if proxyFile != "" {
		files = append(files, proxyFile)
	}
	if v := strings.TrimSpace(os.Getenv("SSO_CONVERT_PROXY_FILE")); v != "" {
		files = append(files, v)
	}
	if v := strings.TrimSpace(os.Getenv("PROXY_POOL_FILE")); v != "" {
		files = append(files, v)
	} else {
		// project default Chinese filename
		files = append(files, "代理.txt")
	}
	if v := strings.TrimSpace(os.Getenv("SSO_CONVERT_PROXY")); v != "" && single == "" {
		for _, p := range splitProxyList(v) {
			add(p)
		}
	}
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			// skip non-proxy share links that need relay (vmess/vless) — only URL-like
			low := strings.ToLower(line)
			if strings.HasPrefix(low, "vmess://") || strings.HasPrefix(low, "vless://") ||
				strings.HasPrefix(low, "trojan://") || strings.HasPrefix(low, "ss://") {
				continue
			}
			add(line)
		}
	}
	if envPool := firstNonEmpty(
		os.Getenv("PROXY_POOL"),
		os.Getenv("PROXY_POOL_LIST"),
		os.Getenv("PROXIES"),
		os.Getenv("PROXY_LIST"),
	); envPool != "" {
		for _, p := range splitProxyList(envPool) {
			add(p)
		}
	}
	// single env proxy last (if pool still empty)
	if len(raw) == 0 {
		if p := firstNonEmpty(os.Getenv("HTTPS_PROXY"), os.Getenv("HTTP_PROXY"), os.Getenv("ALL_PROXY")); p != "" {
			add(p)
		}
	}
	// de-dupe preserve order
	seen := map[string]bool{}
	out := make([]string, 0, len(raw))
	for _, p := range raw {
		if seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	return out
}

func splitProxyList(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\\n", "\n")
	for _, sep := range []string{"\n", ",", ";", "|"} {
		text = strings.ReplaceAll(text, sep, "\n")
	}
	var out []string
	for _, p := range strings.Split(text, "\n") {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func (f *ssoBuildFlow) convert(ctx context.Context, email string) (map[string]any, error) {
	// Warm SSO session (ZhuCe soft-fail: CF may 403 plain Go TLS; continue to device_code).
	status, finalURL, _, err := f.do(ctx, http.MethodGet, accountsHome, nil)
	if err == nil {
		if status == http.StatusUnauthorized || strings.Contains(finalURL, "sign-in") || strings.Contains(finalURL, "sign-up") {
			return nil, errors.New("sso unauthorized (cookie expired or invalid)")
		}
		// 403/5xx: still try device flow — cookie may work on auth.x.ai
	}

	form := url.Values{"client_id": {clientID}, "scope": {scopeDefault}}
	status, _, body, err := f.do(ctx, http.MethodPost, deviceCodeURL, form)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("device_code HTTP %d: %s", status, truncate(string(body), 200))
	}
	var device struct {
		DeviceCode              string `json:"device_code"`
		UserCode                string `json:"user_code"`
		VerificationURIComplete string `json:"verification_uri_complete"`
		VerificationURI         string `json:"verification_uri"`
		Interval                int    `json:"interval"`
		ExpiresIn               int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &device); err != nil {
		return nil, fmt.Errorf("device_code parse: %w", err)
	}
	if device.DeviceCode == "" || device.UserCode == "" {
		return nil, errors.New("device_code incomplete")
	}
	verComplete := device.VerificationURIComplete
	if verComplete == "" {
		base := device.VerificationURI
		if base == "" {
			base = "https://auth.x.ai/oauth2/device"
		}
		if strings.Contains(base, "?") {
			verComplete = base + "&user_code=" + url.QueryEscape(device.UserCode)
		} else {
			verComplete = base + "?user_code=" + url.QueryEscape(device.UserCode)
		}
	}
	if !safeXAIURL(verComplete) {
		return nil, fmt.Errorf("unsafe verification url: %s", verComplete)
	}
	if device.Interval <= 0 {
		device.Interval = 5
	}
	if device.ExpiresIn <= 0 {
		device.ExpiresIn = 1800
	}

	status, finalURL, _, err = f.do(ctx, http.MethodGet, verComplete, nil)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 400 {
		return nil, fmt.Errorf("open device verify page HTTP %d", status)
	}

	status, finalURL, _, err = f.do(ctx, http.MethodPost, deviceVerify, url.Values{
		"user_code": {device.UserCode},
	})
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 400 {
		return nil, fmt.Errorf("device/verify HTTP %d", status)
	}
	if !strings.Contains(finalURL, "consent") {
		// soft: some responses land differently; still try approve
		_ = finalURL
	}

	status, finalURL, _, err = f.do(ctx, http.MethodPost, deviceApprove, url.Values{
		"user_code":      {device.UserCode},
		"action":         {"allow"},
		"principal_type": {"User"},
		"principal_id":   {""},
	})
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 400 {
		return nil, fmt.Errorf("device/approve HTTP %d", status)
	}
	if !strings.Contains(finalURL, "done") && !strings.Contains(finalURL, "consent") {
		// continue to poll — approve may have succeeded without "done" in URL
		_ = finalURL
	}

	tok, err := f.pollToken(ctx, device.DeviceCode, time.Duration(device.Interval)*time.Second, time.Duration(device.ExpiresIn)*time.Second)
	if err != nil {
		return nil, err
	}
	return tokenToCPADoc(email, map[string]any{
		"access_token":  tok.AccessToken,
		"refresh_token": tok.RefreshToken,
		"id_token":      tok.IDToken,
		"expires_in":    float64(tok.ExpiresIn),
		"token_type":    "Bearer",
	}, tokenEndpoint), nil
}

type ssoBuildToken struct {
	AccessToken  string
	RefreshToken string
	IDToken      string
	ExpiresIn    int
}

func (f *ssoBuildFlow) pollToken(ctx context.Context, deviceCode string, interval, expiresIn time.Duration) (ssoBuildToken, error) {
	if interval < time.Second {
		interval = time.Second
	}
	// Approve just finished — poll immediately (don't sleep first interval).
	maxWait := expiresIn
	if maxWait > 45*time.Second {
		maxWait = 45 * time.Second
	}
	deadline := time.Now().Add(maxWait)
	first := true
	for time.Now().Before(deadline) {
		if !first {
			timer := time.NewTimer(interval)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ssoBuildToken{}, ctx.Err()
			case <-timer.C:
			}
		}
		first = false
		status, _, body, err := f.do(ctx, http.MethodPost, tokenEndpoint, url.Values{
			"grant_type":  {"urn:ietf:params:oauth:grant-type:device_code"},
			"client_id":   {clientID},
			"device_code": {deviceCode},
		})
		if err != nil {
			return ssoBuildToken{}, err
		}
		var payload struct {
			AccessToken      string `json:"access_token"`
			RefreshToken     string `json:"refresh_token"`
			IDToken          string `json:"id_token"`
			ExpiresIn        int    `json:"expires_in"`
			Error            string `json:"error"`
			ErrorDescription string `json:"error_description"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			return ssoBuildToken{}, fmt.Errorf("token parse: %w", err)
		}
		if status >= 200 && status < 300 && payload.AccessToken != "" {
			if payload.ExpiresIn <= 0 {
				payload.ExpiresIn = 3600
			}
			return ssoBuildToken{
				AccessToken:  payload.AccessToken,
				RefreshToken: payload.RefreshToken,
				IDToken:      payload.IDToken,
				ExpiresIn:    payload.ExpiresIn,
			}, nil
		}
		switch payload.Error {
		case "authorization_pending":
			continue
		case "slow_down":
			interval += 2 * time.Second
			continue
		case "access_denied":
			return ssoBuildToken{}, errors.New("oauth_denied")
		case "expired_token":
			return ssoBuildToken{}, errors.New("oauth_expired")
		default:
			if status >= 400 {
				return ssoBuildToken{}, fmt.Errorf("oauth token HTTP %d: %s", status, firstNonEmpty(payload.ErrorDescription, payload.Error))
			}
			return ssoBuildToken{}, fmt.Errorf("oauth_rejected: %s", firstNonEmpty(payload.ErrorDescription, payload.Error))
		}
	}
	return ssoBuildToken{}, errors.New("oauth_expired: device flow poll timeout")
}

func (f *ssoBuildFlow) do(ctx context.Context, method, endpoint string, form url.Values) (int, string, []byte, error) {
	if !safeXAIURL(endpoint) {
		return 0, "", nil, fmt.Errorf("unsafe xAI url: %s", endpoint)
	}
	_ = ctx // tls-client request uses client timeout
	currentURL := endpoint
	currentMethod := method
	currentForm := form
	for redirects := 0; redirects <= 8; redirects++ {
		var body io.Reader
		if currentForm != nil {
			body = strings.NewReader(currentForm.Encode())
		}
		req, err := http.NewRequest(currentMethod, currentURL, body)
		if err != nil {
			return 0, "", nil, err
		}
		req.Header = http.Header{
			"Accept":          {"application/json, text/html;q=0.9, */*;q=0.8"},
			"Accept-Language": {"zh-CN,zh;q=0.9,en;q=0.8"},
			"User-Agent":      {userAgent},
			"Cookie":          {f.cookieHeader()},
			http.HeaderOrderKey: {
				"accept", "accept-language", "user-agent", "cookie", "content-type",
			},
		}
		if currentForm != nil {
			req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			req.Header.Set("Origin", "https://accounts.x.ai")
		}
		resp, err := f.client.Do(req)
		if err != nil {
			return 0, "", nil, err
		}
		f.captureCookies(resp)
		data, readErr := io.ReadAll(io.LimitReader(resp.Body, maxAuthBody+1))
		_ = resp.Body.Close()
		if readErr != nil {
			return resp.StatusCode, currentURL, nil, readErr
		}
		if len(data) > maxAuthBody {
			return resp.StatusCode, currentURL, nil, errors.New("xAI response exceeds 2MiB")
		}
		if resp.StatusCode < 300 || resp.StatusCode > 399 {
			return resp.StatusCode, currentURL, data, nil
		}
		location := strings.TrimSpace(resp.Header.Get("Location"))
		if location == "" {
			return resp.StatusCode, currentURL, data, errors.New("redirect missing Location")
		}
		base, _ := url.Parse(currentURL)
		next, err := url.Parse(location)
		if err != nil {
			return resp.StatusCode, currentURL, data, err
		}
		currentURL = base.ResolveReference(next).String()
		if !safeXAIURL(currentURL) {
			return resp.StatusCode, currentURL, data, fmt.Errorf("redirect to untrusted host: %s", currentURL)
		}
		// 303 / 301+302 on non-GET → switch to GET (RFC + grok2api behavior)
		if resp.StatusCode == http.StatusSeeOther ||
			((resp.StatusCode == http.StatusMovedPermanently || resp.StatusCode == http.StatusFound) &&
				currentMethod != http.MethodGet && currentMethod != http.MethodHead) {
			currentMethod = http.MethodGet
			currentForm = nil
		}
	}
	return 0, currentURL, nil, errors.New("too many redirects")
}

func (f *ssoBuildFlow) captureCookies(resp *http.Response) {
	for _, c := range resp.Cookies() {
		name := strings.TrimSpace(c.Name)
		value := strings.TrimSpace(c.Value)
		if name == "" || len(name) > 128 || len(value) > 16384 {
			continue
		}
		if strings.ContainsAny(name+value, "\r\n\x00") {
			continue
		}
		if c.MaxAge < 0 {
			delete(f.cookies, name)
			continue
		}
		f.cookies[name] = value
	}
}

func (f *ssoBuildFlow) cookieHeader() string {
	keys := make([]string, 0, len(f.cookies))
	for k := range f.cookies {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+f.cookies[k])
	}
	return strings.Join(parts, "; ")
}

func safeXAIURL(raw string) bool {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Hostname() == "" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	return host == "x.ai" || strings.HasSuffix(host, ".x.ai")
}

func normalizeSSOToken(value string) string {
	value = strings.TrimSpace(value)
	if strings.HasPrefix(strings.ToLower(value), "sso=") {
		value = strings.TrimSpace(value[len("sso="):])
	}
	if token, _, found := strings.Cut(value, ";"); found {
		value = strings.TrimSpace(token)
	}
	return strings.NewReplacer("\r", "", "\n", "", "\x00", "").Replace(value)
}

func tokenToCPADoc(email string, tok map[string]any, endpoint string) map[string]any {
	at := strAny(tok["access_token"])
	rt := strAny(tok["refresh_token"])
	idTok := strAny(tok["id_token"])
	expiresIn := 3600
	if v, ok := tok["expires_in"].(float64); ok {
		expiresIn = int(v)
	}
	now := time.Now().UTC()
	expired := now.Add(time.Duration(expiresIn) * time.Second).Format(time.RFC3339)
	sub := jwtSubject(idTok)
	if sub == "" {
		sub = jwtSubject(at)
	}
	if email == "" {
		email = jwtEmail(idTok)
	}
	return map[string]any{
		"type":           "xai",
		"access_token":   at,
		"refresh_token":  rt,
		"id_token":       idTok,
		"token_type":     firstNonEmpty(strAny(tok["token_type"]), "Bearer"),
		"expires_in":     expiresIn,
		"expired":        expired,
		"last_refresh":   now.Format(time.RFC3339),
		"sub":            sub,
		"base_url":       cliBase,
		"token_endpoint": firstNonEmpty(endpoint, tokenEndpoint),
		"auth_kind":      "oauth",
		"email":          email,
		"headers": map[string]string{
			"X-XAI-Token-Auth":         "xai-grok-cli",
			"x-grok-client-version":    "0.2.93",
			"x-grok-client-identifier": "grok-shell",
		},
	}
}

func jwtSubject(token string) string {
	claims := jwtClaims(token)
	for _, k := range []string{"sub", "principal_id"} {
		if v := strAny(claims[k]); v != "" {
			return v
		}
	}
	return ""
}

func jwtEmail(token string) string {
	return strAny(jwtClaims(token)["email"])
}

func jwtClaims(token string) map[string]any {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return nil
	}
	payload := parts[1]
	switch len(payload) % 4 {
	case 2:
		payload += "=="
	case 3:
		payload += "="
	}
	raw, err := base64.URLEncoding.DecodeString(payload)
	if err != nil {
		raw, err = base64.RawURLEncoding.DecodeString(parts[1])
		if err != nil {
			return nil
		}
	}
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return m
}

// ---------- indexes / loaders ----------

func indexCPA(root string) map[string]map[string]any {
	out := map[string]map[string]any{}
	dir := filepath.Join(root, "cpa")
	ents, err := os.ReadDir(dir)
	if err != nil {
		return out
	}
	for _, e := range ents {
		n := e.Name()
		if !strings.HasPrefix(n, "xai-") || !strings.HasSuffix(n, ".json") {
			continue
		}
		doc := readJSONMap(filepath.Join(dir, n))
		if doc == nil {
			continue
		}
		email := strings.ToLower(strings.TrimSpace(firstNonEmpty(strAny(doc["email"]), strAny(doc["name"]))))
		if email != "" {
			out[email] = doc
		}
	}
	return out
}

func indexSub2API(root string) map[string]map[string]any {
	out := map[string]map[string]any{}
	dir := filepath.Join(root, "sub2api")
	ents, err := os.ReadDir(dir)
	if err != nil {
		return out
	}
	for _, e := range ents {
		n := e.Name()
		if !strings.HasSuffix(n, ".sub2api.json") || n == "accounts.sub2api.json" {
			continue
		}
		doc := readJSONMap(filepath.Join(dir, n))
		if doc == nil {
			continue
		}
		items, _ := doc["accounts"].([]any)
		for _, raw := range items {
			item, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			creds, _ := item["credentials"].(map[string]any)
			extra, _ := item["extra"].(map[string]any)
			if creds == nil {
				creds = map[string]any{}
			}
			if extra == nil {
				extra = map[string]any{}
			}
			email := strings.ToLower(strings.TrimSpace(firstNonEmpty(
				strAny(creds["email"]), strAny(extra["email"]), strAny(item["name"]),
			)))
			if email != "" {
				out[email] = item
			}
		}
	}
	return out
}

type legacyAcc struct {
	Email    string // optional; set when loaded from --sso-file
	Password string
	SSO      string
}

func loadLegacyAccounts(root string) map[string]legacyAcc {
	out := map[string]legacyAcc{}
	b, err := os.ReadFile(filepath.Join(root, "accounts.txt"))
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, ":")
		if len(parts) < 3 {
			continue
		}
		email := strings.ToLower(strings.TrimSpace(parts[0]))
		out[email] = legacyAcc{Password: parts[1], SSO: parts[2]}
	}
	return out
}

type sessionAcc struct {
	SSO string
}

func loadAuthSessions(root string) map[string]sessionAcc {
	out := map[string]sessionAcc{}
	b, err := os.ReadFile(filepath.Join(root, "auth-sessions.jsonl"))
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var doc map[string]any
		if json.Unmarshal([]byte(line), &doc) != nil {
			continue
		}
		email := strings.ToLower(strings.TrimSpace(strAny(doc["email"])))
		if email == "" {
			continue
		}
		sso := ""
		if cookies, ok := doc["cookies"].([]any); ok {
			for _, c := range cookies {
				cm, ok := c.(map[string]any)
				if !ok {
					continue
				}
				if strAny(cm["name"]) == "sso" && strAny(cm["value"]) != "" {
					sso = strAny(cm["value"])
					break
				}
			}
		}
		if sso != "" {
			out[email] = sessionAcc{SSO: sso}
		}
	}
	return out
}

func loadOrCreateSalt(root string) []byte {
	if v := strings.TrimSpace(os.Getenv("XAI_ENROLLER_SOURCE_SALT")); v != "" {
		return []byte(v)
	}
	path := filepath.Join(root, ".xai-enroller-salt")
	if b, err := os.ReadFile(path); err == nil {
		s := strings.TrimSpace(string(b))
		if s != "" {
			return []byte(s)
		}
	}
	_ = os.MkdirAll(root, 0o700)
	buf := make([]byte, 32)
	_, _ = rand.Read(buf)
	val := base64.RawURLEncoding.EncodeToString(buf)
	_ = os.WriteFile(path, []byte(val+"\n"), 0o600)
	return []byte(val)
}

// ---------- utils ----------

func writeJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func atomicWriteJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func readJSONMap(path string) map[string]any {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var m map[string]any
	if json.Unmarshal(b, &m) != nil {
		return nil
	}
	return m
}

func mtimeISO(path string) string {
	st, err := os.Stat(path)
	if err != nil {
		return ""
	}
	return st.ModTime().UTC().Format(time.RFC3339)
}

func fileExists(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.Mode().IsRegular()
}

func strAny(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case fmt.Stringer:
		return t.String()
	case float64:
		return strconv.FormatFloat(t, 'f', -1, 64)
	case json.Number:
		return t.String()
	case nil:
		return ""
	default:
		return fmt.Sprint(t)
	}
}

func firstNonEmpty(ss ...string) string {
	for _, s := range ss {
		if strings.TrimSpace(s) != "" {
			return s
		}
	}
	return ""
}

func hmacHex(secret []byte, msg string) string {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(msg))
	return hex.EncodeToString(mac.Sum(nil))
}

func uniqueStrings(in []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(in))
	for _, s := range in {
		if seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
