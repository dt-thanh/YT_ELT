"""
CLI - điểm vào DUY NHẤT để chạy pipeline.

    python -m youtube_intel.cli collect
    python -m youtube_intel.cli collect --channel UCXLes... --dry-run
    python -m youtube_intel.cli replay --collection-id <uuid>
    python -m youtube_intel.cli status
    python -m youtube_intel.cli verify-channels

VÌ SAO CLI QUAN TRỌNG HƠN NGƯỜI TA TƯỞNG:

1. Airflow chỉ còn là BỘ HẸN GIỜ. DAG ở Bước 9 sẽ gọi đúng những lệnh này.
   Logic nghiệp vụ KHÔNG nằm trong Airflow -> đổi sang Dagster/Prefect/cron
   chỉ cần đổi wrapper.

2. Debug được như code Python bình thường. Không phải vào UI, bật DAG, chờ
   scheduler, rồi đọc log qua trình duyệt.

3. CI chạy được mà không cần dựng Airflow.

4. Con người vận hành được lúc 2 giờ sáng khi sự cố.

MÃ THOÁT (exit code) LÀ MỘT PHẦN CỦA GIAO DIỆN:
    0 = thành công     -> Airflow/CI coi là PASS
    1 = có vấn đề      -> Airflow retry, CI báo đỏ
    2 = lỗi cấu hình   -> retry vô ích, cần CON NGƯỜI sửa
Chương trình CLI mà luôn trả 0 là chương trình không ai giám sát được.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from youtube_intel.config import ConfigError, load_config
from youtube_intel import pipeline, repository as repo


def setup_logging(verbose: bool = False, as_json: bool = False) -> None:
    """Cấu hình log.

    --json bật STRUCTURED LOGGING: mỗi dòng là một JSON. Người đọc thì khó hơn,
    nhưng MÁY đọc được -> gom vào Elasticsearch/Loki rồi truy vấn
    "cho tôi mọi lỗi quota trong 7 ngày qua". Log dạng chuỗi tự do thì phải
    dùng regex để bới, rất mong manh.
    """
    level = logging.DEBUG if verbose else logging.INFO
    if as_json:
        fmt = '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
    else:
        fmt = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", stream=sys.stderr)
    # botocore/urllib3 nói quá nhiều ở mức INFO -> hạ xuống WARNING.
    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# =============================================================================
# LỆNH
# =============================================================================

def cmd_collect(args) -> int:
    # collect gọi YouTube, ghi MinIO và database -> cần cả ba nhóm.
    cfg = load_config(require=("youtube", "minio", "db"))
    result = pipeline.run_collection(
        cfg,
        only_channels=args.channel or None,
        scheduled_for=datetime.fromisoformat(args.scheduled_for) if args.scheduled_for else None,
        collection_id=args.collection_id,
        dry_run=args.dry_run,
    )
    print(result.summary())
    if args.json:
        print(json.dumps({
            "collection_id": result.collection_id,
            "status": result.status,
            "quota_units": result.total_quota_units,
            "channels": [o.__dict__ for o in result.outcomes],
        }, default=str, ensure_ascii=False, indent=2))
    return result.exit_code()


def cmd_replay(args) -> int:
    # ⭐ replay KHÔNG gọi YouTube -> KHÔNG đòi API key.
    # Đòi thứ không dùng là chặn một thao tác hợp lệ (least privilege cho config).
    cfg = load_config(require=("minio", "db"))
    result = pipeline.replay_collection(cfg, collection_id=args.collection_id,
                                        attempt=args.attempt)
    print(result.summary())
    print("\n(replay KHÔNG gọi YouTube API - 0 quota)")
    return result.exit_code()


def cmd_status(args) -> int:
    """Xem các lần chạy gần nhất. Lệnh đầu tiên bạn gõ khi có sự cố."""
    cfg = load_config(require=("db",))          # status chỉ đọc database
    s = cfg.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)

    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute("""
                SELECT r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
                       r.status, r.code_version,
                       count(b.batch_id)                                        AS batches,
                       count(*) FILTER (WHERE b.quality_status = 'passed')      AS passed,
                       count(*) FILTER (WHERE b.quality_status = 'quarantined') AS quarantined,
                       sum(b.received_count)                                    AS rows_recv
                  FROM yti.collection_runs r
                  LEFT JOIN yti.channel_batches b USING (collection_id)
                 GROUP BY r.collection_id, r.scheduled_for, r.started_at, r.ended_at,
                          r.status, r.code_version
                 ORDER BY r.started_at DESC
                 LIMIT %s;
            """, (args.limit,))
            runs = cur.fetchall()

            print(f"{'collection':<12}{'lịch hẹn':<18}{'trạng thái':<12}"
                  f"{'batch':>6}{'pass':>6}{'quar':>6}{'dòng':>8}  code")
            print("-" * 84)
            for r in runs:
                print(f"{str(r['collection_id'])[:8]:<12}"
                      f"{r['scheduled_for'].strftime('%m-%d %H:%M'):<18}"
                      f"{r['status']:<12}{r['batches']:>6}{r['passed'] or 0:>6}"
                      f"{r['quarantined'] or 0:>6}{r['rows_recv'] or 0:>8}  {r['code_version'] or '-'}")

            cur.execute("""
                SELECT (SELECT count(*) FROM yti.videos_current)     AS videos,
                       (SELECT count(*) FROM yti.video_observations) AS obs,
                       (SELECT count(DISTINCT collection_id) FROM yti.video_observations) AS collections
            """)
            t = cur.fetchone()
            print(f"\nĐÃ CÔNG BỐ: {t['videos']} video · {t['obs']} observation "
                  f"· {t['collections']} lần thu")

            # Cần >= 2 observation cho cùng video thì mới tính được tăng trưởng.
            # Đây là thứ KHÔNG rút ngắn được bằng cách cố gắng hơn - phải chờ.
            cur.execute("""
                SELECT count(*) AS n FROM (
                    SELECT video_id FROM yti.video_observations
                    GROUP BY video_id HAVING count(*) >= 2
                ) x
            """)
            print(f"Video có >= 2 lần quan sát (tính được tăng trưởng): "
                  f"{cur.fetchone()['n']}")
    return 0


def cmd_report(args) -> int:
    """Sinh bản tin cho từng nhóm.

    CHỈ đòi database. KHÔNG đòi OPENAI_API_KEY, kể cả khi llm_enabled=true.

    ⭐ VÌ SAO KHÔNG FAIL-FAST KHI THIẾU KEY?
    Fail-fast đúng với thứ BẮT BUỘC. LLM là TÙY CHỌN: thiếu key thì vẫn có bản
    mẫu với số liệu đúng. Nếu gãy ở đây, DAG sẽ đỏ và ngày đó KHÔNG CÓ bản tin
    nào - một tính năng phụ làm hỏng tính năng chính.
    Thay vào đó: cảnh báo TO, và cột status = 'template' trong yti.reports
    ghi lại sự thật - không có gì bị che giấu.
    """
    from dataclasses import replace
    from youtube_intel import reporting

    cfg = load_config(require=("db",))

    if args.no_llm:
        # dataclasses.replace: tạo bản sao với một trường khác đi.
        # Config là frozen (bất biến) nên không gán trực tiếp được - đúng ý đồ.
        cfg = replace(cfg, settings=replace(cfg.settings, llm_enabled=False))
    elif cfg.settings.llm_enabled and not cfg.secrets.openai_api_key:
        logging.getLogger(__name__).warning(
            "llm_enabled=true nhưng THIẾU OPENAI_API_KEY -> dùng BẢN MẪU. "
            "Thêm key vào .env rồi chạy `docker compose up -d` để bật LLM.")

    s = cfg.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)

    outcomes = reporting.run_reports(cfg, db, only_group=args.group)

    for o in outcomes:
        print(f"\n{'='*70}\n[{o.group}] {o.status.upper()}")
        if o.error:
            print(f"  {o.error}")
            continue
        cov = o.coverage
        print(f"  ứng viên  : {o.candidates}"
              f"  | bao phủ {cov.get('rankable')}/{cov.get('total_videos')}")
        if o.tokens_used:
            print(f"  LLM       : {o.tokens_used} token, {o.latency_ms} ms, "
                  f"{o.attempts} lần thử")
        print(f"  report_id : {o.report_id}")
        if args.show:
            print("\n" + o.markdown)

    bad = [o for o in outcomes if o.status in ("failed",)]
    return 1 if bad else 0


def cmd_eval(args) -> int:
    """Chấm một phiên bản prompt trên 20 ca thử tổng hợp. KHÔNG cần database.

    Chỉ cần key OpenAI -> chạy được trên máy dev, không cần Docker.
    Đây là phần thưởng của việc tách logic khỏi hạ tầng từ Buổi 2.
    """
    from pathlib import Path
    from youtube_intel import evals

    from youtube_intel.llm import LLMClient, NullSink, PostgresSink

    cfg = load_config(require=("llm",))
    s = cfg.secrets
    # Log eval calls to yti.llm_calls when database settings are present; eval
    # itself still runs without a database (logging is optional here).
    if all((s.db_host, s.db_port, s.db_name, s.db_user, s.db_password)):
        sink = PostgresSink(repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                                          user=s.db_user, password=s.db_password))
    else:
        logging.getLogger(__name__).info("No database settings - eval calls are not logged.")
        sink = NullSink()
    client = LLMClient(api_key=s.openai_api_key, model=cfg.settings.llm_model,
                       pricing=cfg.settings.llm_pricing, sink=sink)
    result = evals.run_eval(settings=cfg.settings, client=client,
                            prompt_version=args.prompt_version, limit=args.limit)
    path = evals.save_result(result, Path(args.out))

    s = result["summary"]
    print(f"\n=== EVAL prompt {result['prompt_version']} ({result['model']}) ===")
    print(f"  Ca đạt  : {s['cases_passed']}/{s['cases_total']}  ({s['pass_rate']:.0%})")
    print(f"  Sửa lại : {s['repairs']} ca phải gọi LLM lần 2")
    print(f"  Token   : {result['tokens_used']:,}")

    base = None
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))["summary"]
        print(f"\n  {'kiểm tra':<22}{base_label(args.baseline):>12}{args.prompt_version:>12}")
    else:
        print(f"\n  {'kiểm tra':<22}{'đạt':>12}")
    for name, v in s["per_check"].items():
        now = f"{v['passed']}/{v['applicable']}" if v["applicable"] else "-"
        if base:
            b = base["per_check"].get(name, {})
            old = f"{b.get('passed')}/{b.get('applicable')}" if b.get("applicable") else "-"
            print(f"  {name:<22}{old:>12}{now:>12}")
        else:
            print(f"  {name:<22}{now:>12}")

    failed = [r["case_id"] for r in result["cases"] if not r["passed"]]
    if failed:
        print(f"\n  Ca trượt: {', '.join(failed)}  (xem chi tiết trong file kết quả)")
    print(f"\n  Lưu: {path}")
    return 0


def base_label(path: str) -> str:
    """'evals/results/v1-20260924T071500.json' -> 'v1' (nhãn cột so sánh)."""
    from pathlib import Path
    return Path(path).name.split("-")[0]


def cmd_ask(args) -> int:
    """Ask the assistant one question. Tools read the database as a read-only login."""
    from youtube_intel.assistant.agent import build_assistant

    cfg = load_config(require=("db", "llm", "assistant"))
    answer = build_assistant(cfg).ask(args.question, session_id=args.session)

    label = {"answered": "verified", "declined": "declined", "fallback": "FALLBACK",
             "failed": "FAILED"}[answer.status]
    if answer.status == "answered" and answer.repaired:
        label = "verified after one repair"
    print(answer.text)
    if answer.citations:
        print("\nSources:")
        for c in answer.citations:
            print(f"  - {c.title or c.video_id}  ({c.channel_title or '-'})  {c.url}")
    if answer.data_gaps:
        print("\nData gaps:")
        for g in answer.data_gaps:
            print(f"  - {g}")
    cost = f"~${answer.est_cost_usd:.5f}" if answer.est_cost_usd is not None else "cost unknown"
    print(f"\n[{label}] data as of {answer.data_as_of or '-'} · {answer.steps} LLM round(s) · "
          f"{(answer.input_tokens or 0) + (answer.output_tokens or 0):,} tokens · {cost} · "
          f"{answer.latency_ms} ms")

    if args.trace:
        print(f"\nsession_id={answer.session_id}  turn_id={answer.turn_id}  "
              f"prompt={answer.prompt_version}")
        print("Tool trace:")
        for t in answer.tool_trace:
            err = f"  ERROR={t.error}" if t.error else ""
            print(f"  step {t.step}: {t.tool}({json.dumps(t.args, ensure_ascii=False)}) "
                  f"-> {t.row_count} rows, {t.latency_ms} ms{err}")
        if answer.validation_errors:
            print("Grounding errors:")
            for e in answer.validation_errors:
                print(f"  - {e}")
    return 1 if answer.status == "failed" else 0


def _rag_clients(cfg, database: str | None = None):
    """(data db, LLMClient with the RAG embedding model).

    LLM calls are always logged in the MAIN database; `database` only changes
    where chunks are read/written (e.g. yti_eval for the assistant evaluation).
    """
    from youtube_intel.llm import LLMClient, PostgresSink
    s, st = cfg.secrets, cfg.settings
    main = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                         user=s.db_user, password=s.db_password)
    data = repo.Database(host=s.db_host, port=s.db_port, name=database or s.db_name,
                         user=s.db_user, password=s.db_password)
    client = (LLMClient(api_key=s.openai_api_key, model=st.llm_model, pricing=st.llm_pricing,
                        sink=PostgresSink(main), embedding_model=st.rag.embedding_model)
              if s.openai_api_key else None)
    return data, client


def cmd_embed(args) -> int:
    """Chunk + embed video titles/descriptions into yti.video_chunks (only new/changed)."""
    from youtube_intel.rag.indexer import index_videos

    cfg = load_config(require=("db",) if args.dry_run else ("db", "llm"))
    db, client = _rag_clients(cfg, args.database)
    stats = index_videos(db, client, cfg.settings.rag, dry_run=args.dry_run)
    mode = "DRY RUN - nothing embedded or written" if stats.dry_run else "done"
    print(f"=== embed into {db.name} ({cfg.settings.rag.embedding_model}): {mode} ===")
    print(f"  videos      : {stats.videos}")
    print(f"  chunks      : {stats.chunks}  (unchanged {stats.unchanged}, to embed {stats.to_embed})")
    print(f"  removed     : {stats.removed} stale chunk(s)")
    if not stats.dry_run:
        print(f"  embedded    : {stats.embedded}")
    return 0


def cmd_eval_rag(args) -> int:
    """recall@5 / MRR@5 of vector, full-text and hybrid search on labeled queries."""
    from pathlib import Path
    from youtube_intel.rag import evaluation as ev

    cfg = load_config(require=("db", "llm"))
    db, client = _rag_clients(cfg)
    queries = ev.load_queries(Path(args.queries))

    if args.suggest:
        sugg = ev.suggest(db=db, client=client, rag=cfg.settings.rag, queries=queries)
        path = ev.save(sugg, Path(args.out), "rag-suggestions")
        print(f"Candidates for {len(sugg)} unlabeled queries -> {path}")
        print("Review them and copy the truly relevant video ids into", args.queries)
        return 0

    result = ev.run_rag_eval(db=db, client=client, rag=cfg.settings.rag, queries=queries)
    if not result["n_labeled"]:
        print(f"No labeled queries in {args.queries}. Run with --suggest, label, then retry.")
        return 1
    path = ev.save(result, Path(args.out), "rag")
    print(f"=== RAG eval: {result['n_labeled']}/{result['n_queries']} labeled queries, "
          f"k=5, {result['embedding_model']} ===")
    print(f"  {'mode':<10}{'recall@5':>10}{'MRR@5':>8}")
    for mode, m in result["summary"].items():
        print(f"  {mode:<10}{m['recall@5']:>10.3f}{m['mrr@5']:>8.3f}")
    print(f"\n  Saved: {path}")
    return 0


def cmd_eval_assistant(args) -> int:
    """Run the assistant on the fixed synthetic database and score it (see evaluation.py)."""
    from pathlib import Path
    from youtube_intel.assistant import evaluation as ev
    from youtube_intel.assistant.agent import build_assistant

    cfg = load_config(require=("db", "llm", "assistant"))
    s = cfg.secrets
    eval_db = repo.Database(host=s.db_host, port=s.db_port, name=args.database,
                            user=s.db_user, password=s.db_password)
    assistant = build_assistant(cfg, data_db_name=args.database)
    cases = ev.load_cases(Path(args.cases), known_tools=assistant.registry.names)

    if args.rescore:
        result = ev.rescore(json.loads(Path(args.rescore).read_text(encoding="utf-8")), cases)
        path = Path(args.rescore).with_name(Path(args.rescore).stem + "-rescored.json")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        _print_eval_summary(result)
        print(f"\n  Re-scored without calling the LLM -> {path}")
        return 0

    if not args.no_reseed:
        ev.load_seed(eval_db, Path(args.seed))
    skip = ()
    with eval_db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute("SELECT to_regclass('yti.video_chunks') IS NOT NULL AS has_table")
            has_table = cur.fetchone()["has_table"]
            n_chunks = 0
            if has_table:
                cur.execute("SELECT count(*) AS n FROM yti.video_chunks")
                n_chunks = cur.fetchone()["n"]
    if not n_chunks:
        skip = ("rag",)
        print(f"  (no chunks in {args.database}: RAG cases skipped - run "
              f"`youtube-intel embed --database {args.database}` first)")

    chosen = ev.select(cases, split=args.split, limit=args.limit, skip_categories=skip)
    version = args.prompt_version or cfg.settings.assistant.prompt_version
    print(f"Running {len(chosen)} case(s) x {args.repeat} on {args.database}, prompt {version} ...")
    records = ev.run_cases(assistant, chosen, prompt_version=version, repeat=args.repeat,
                           progress=lambda r: print(f"  {r['case_id']:<4} run {r['run_index']}  "
                                                    f"{'PASS' if r['passed'] else 'FAIL'}  {r['status']}"))
    result = ev.build_result(records, prompt_version=version, model=cfg.settings.llm_model,
                             split=args.split, repeat=args.repeat)
    if args.baseline:
        result["comparison"] = ev.compare(
            json.loads(Path(args.baseline).read_text(encoding="utf-8")), result)
    path = ev.save(result, Path(args.out))
    _print_eval_summary(result)
    print(f"\n  Saved: {path}")
    return 0


def _print_eval_summary(result: dict) -> None:
    s = result["summary"]
    ci = s["pass_rate_ci95"]
    print(f"\n=== assistant eval: prompt {result['prompt_version']} ({result['model']}), "
          f"split {result['split']}, repeat {result['repeat']}, synthetic data ===")
    print(f"  cases passed : {s['passed']}/{s['records']} ({s['pass_rate']:.0%}), "
          f"95% CI {ci[0]:.0%}-{ci[1]:.0%}" if ci else "  no records")
    if s["runs"] > 1:
        print(f"  per run      : {s['pass_rate_per_run']}  flaky: {s['flaky_cases'] or '-'}")
    print(f"  repaired {s['repair_rate']:.0%} · fallback {s['fallback_rate']:.0%} · "
          f"p50 {s['latency_ms_p50']} ms · p95 {s['latency_ms_p95']} ms · "
          f"avg {s['avg_tokens']} tokens · avg ${s['avg_cost_usd'] or 0:.5f}/question")
    print(f"\n  {'scorer':<20}{'pass':>10}")
    for name, v in s["per_scorer"].items():
        print(f"  {name:<20}{(str(v['passed']) + '/' + str(v['applicable'])) if v['applicable'] else '-':>10}")
    print(f"\n  {'category':<20}{'pass':>10}")
    for name, v in s["per_category"].items():
        print(f"  {name:<20}{v['passed']:>5}/{v['total']}")
    cmp_ = result.get("comparison")
    if cmp_:
        print(f"\n  vs baseline: pass rate {cmp_['pass_rate_delta']:+.1%}, "
              f"regressions {cmp_['regressions'] or '-'}, fixes {cmp_['fixes'] or '-'}")


def cmd_llm_stats(args) -> int:
    """Daily LLM usage from yti.v_llm_daily: calls, tokens, cost, latency, failure rates."""
    cfg = load_config(require=("db",))
    s = cfg.secrets
    db = repo.Database(host=s.db_host, port=s.db_port, name=s.db_name,
                       user=s.db_user, password=s.db_password)
    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            cur.execute(
                """
                SELECT * FROM yti.v_llm_daily
                 WHERE day > (now() AT TIME ZONE 'UTC')::date - %s
                 ORDER BY day DESC, feature, model;
                """,
                (args.days,),
            )
            rows = cur.fetchall()

    print(f"=== LLM usage, last {args.days} day(s), UTC ===")
    if not rows:
        print("  No LLM calls in this period.")
        return 0

    def pct(v):
        return "-" if v is None else f"{float(v):.0%}"

    def num(v, fmt):
        return "-" if v is None else format(float(v), fmt)

    print(f"{'day':<11}{'feature':<10}{'model':<24}{'calls':>6}{'in_tok':>9}{'out_tok':>8}"
          f"{'cost_usd':>10}{'p50_ms':>8}{'p95_ms':>8}{'err':>6}{'val_fail':>9}{'fallbk':>7}")
    print("-" * 116)
    for r in rows:
        cost = num(r["est_cost_usd"], ".4f") + ("*" if r["calls_without_cost"] else "")
        print(f"{r['day']!s:<11}{r['feature']:<10}{r['model'][:23]:<24}{r['calls']:>6}"
              f"{num(r['input_tokens'], ',.0f'):>9}{num(r['output_tokens'], ',.0f'):>8}"
              f"{cost:>10}{num(r['latency_p50_ms'], '.0f'):>8}{num(r['latency_p95_ms'], '.0f'):>8}"
              f"{pct(r['error_rate']):>6}{pct(r['validation_fail_rate']):>9}{pct(r['fallback_rate']):>7}")

    total_calls = sum(r["calls"] for r in rows)
    total_cost = sum(float(r["est_cost_usd"] or 0) for r in rows)
    print("-" * 116)
    print(f"TOTAL: {total_calls} calls, ~${total_cost:.4f}")
    if any(r["calls_without_cost"] for r in rows):
        print("  * some calls have no price in llm_pricing - their cost is unknown, "
              "so this total is a lower bound.")
    return 0


def cmd_retention(args) -> int:
    """Dọn dẹp dữ liệu quá hạn. MẶC ĐỊNH CHỈ XEM TRƯỚC.

    ⭐ Phải gõ --apply mới xóa thật. Thao tác KHÔNG HOÀN TÁC ĐƯỢC thì không bao
    giờ là hành vi mặc định - đây là mẫu PLAN/APPLY, giống terraform.
    """
    from youtube_intel import retention
    from youtube_intel.storage import RawStore

    cfg = load_config(require=("minio", "db"))     # không cần API key
    st, sec = cfg.settings, cfg.secrets
    store = RawStore(endpoint_url=sec.minio_endpoint, access_key=sec.minio_access_key,
                     secret_key=sec.minio_secret_key)
    db = repo.Database(host=sec.db_host, port=sec.db_port, name=sec.db_name,
                       user=sec.db_user, password=sec.db_password)

    mode = "ÁP DỤNG (xóa thật)" if args.apply else "XEM TRƯỚC (không xóa gì)"
    print(f"=== RETENTION - {mode} ===")
    print(f"  staging_retention_days = {st.staging_retention_days}")
    print(f"  orphan_retention_hours = {st.orphan_retention_hours}")
    print(f"  raw_retention_days     = {st.raw_retention_days}\n")

    with db.connect() as conn:
        with repo.dict_cursor(conn) as cur:
            staging_plan = retention.plan_staging_cleanup(cur, days=st.staging_retention_days)
    orphan_plan = retention.plan_orphan_cleanup(store, hours=st.orphan_retention_hours)
    raw_plan = retention.plan_raw_cleanup(store, days=st.raw_retention_days)

    for plan in (staging_plan, orphan_plan, raw_plan):
        print(plan.describe())

    total = staging_plan.count + orphan_plan.count + raw_plan.count
    freed = orphan_plan.bytes_freed + raw_plan.bytes_freed
    print(f"\n  TỔNG: {total} mục" + (f", giải phóng ~{retention._human(freed)}" if freed else ""))

    if not args.apply:
        print("\n  (chưa xóa gì. Thêm --apply để thực hiện)")
        return 0

    print("\n=== ĐANG XÓA ===")
    with db.connect() as conn:
        with conn:
            with repo.dict_cursor(conn) as cur:
                n = retention.delete_staging(cur, days=st.staging_retention_days)
                print(f"  staging : xóa {n} dòng")
    for plan in (orphan_plan, raw_plan):
        if plan.keys:
            print(f"  {plan.what}: xóa {store.delete_keys(plan.keys)} object")
    return 0


def cmd_verify_channels(args) -> int:
    """Xác minh mọi channel_id trong config bằng API. ~1 unit/kênh."""
    from youtube_intel.youtube import YouTubeClient
    cfg = load_config(require=("youtube",))   # chỉ gọi API, không chạm MinIO/DB
    bad = 0
    with YouTubeClient(cfg.secrets.youtube_api_key,
                       timeout=cfg.settings.request_timeout_seconds,
                       max_attempts=cfg.settings.max_attempts) as yt:
        for c in cfg.channels:
            try:
                info = yt.fetch_channel(channel_id=c.channel_id)
                flag = "OK " if info.title == c.title else "KHÁC TÊN"
                print(f"  [{flag}] {c.channel_id}  {info.title}  "
                      f"({info.video_count} video, uploads={info.uploads_playlist_id})")
                if info.title != c.title:
                    print(f"          config ghi: {c.title!r}")
            except Exception as e:
                bad += 1
                print(f"  [LỖI] {c.channel_id}: {e}")
        print(f"\nQuota đã dùng: {yt.meter.estimated_units} units")
    return 1 if bad else 0


# =============================================================================
# PHÂN TÍCH THAM SỐ
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_intel",
        description="YouTube Content Intelligence - thu thập và xuất bản dữ liệu kênh.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log mức DEBUG")
    p.add_argument("--json-logs", action="store_true", help="log dạng JSON (cho máy đọc)")

    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="Thu thập từ YouTube API (TỐN QUOTA)")
    c.add_argument("--channel", action="append",
                   help="chỉ chạy channel_id này (lặp lại được)")
    c.add_argument("--scheduled-for", help="ô lịch dạng ISO, vd 2026-09-22T12:00:00+00:00")
    c.add_argument("--collection-id", help="dùng lại collection_id có sẵn (để chạy lại)")
    c.add_argument("--dry-run", action="store_true",
                   help="gọi API và in kết quả nhưng KHÔNG ghi MinIO/database")
    c.add_argument("--json", action="store_true", help="in thêm kết quả dạng JSON")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("replay", help="Nạp lại từ raw đã có (0 QUOTA, không gọi mạng)")
    r.add_argument("--collection-id", required=True)
    r.add_argument("--attempt", type=int, default=1)
    r.set_defaults(func=cmd_replay)

    s = sub.add_parser("status", help="Xem các lần chạy gần nhất")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_status)

    rp = sub.add_parser("report", help="Sinh bản tin cho từng nhóm (dùng LLM nếu bật)")
    rp.add_argument("--group", help="chỉ một nhóm")
    rp.add_argument("--no-llm", action="store_true",
                    help="bỏ qua LLM, dùng bản mẫu (không cần OPENAI_API_KEY, 0đ)")
    rp.add_argument("--show", action="store_true", help="in cả nội dung markdown")
    rp.set_defaults(func=cmd_report)

    ev = sub.add_parser("eval", help="Chấm prompt trên 20 ca tổng hợp (TỐN TIỀN OpenAI, ~0,01 USD)")
    ev.add_argument("--prompt-version", required=True, help="vd v1, v2")
    ev.add_argument("--limit", type=int, help="chỉ chạy N ca đầu (thử nhanh)")
    ev.add_argument("--out", default="evals/results", help="thư mục lưu kết quả")
    ev.add_argument("--baseline", help="file kết quả cũ để so sánh")
    ev.set_defaults(func=cmd_eval)

    ak = sub.add_parser("ask", help="Ask the data assistant a question (OpenAI, a few cents at most)")
    ak.add_argument("question", help='e.g. "Video nào tăng view nhanh nhất nhóm kids_animation?"')
    ak.add_argument("--trace", action="store_true", help="show tools called and grounding errors")
    ak.add_argument("--session", help="session id to group several questions in the LLM log")
    ak.set_defaults(func=cmd_ask)

    em = sub.add_parser("embed", help="Embed video titles/descriptions for search (OpenAI, cheap)")
    em.add_argument("--dry-run", action="store_true", help="only count what would be embedded")
    em.add_argument("--database", help="database to index (default: the main one; yti_eval for eval)")
    em.set_defaults(func=cmd_embed)

    er = sub.add_parser("eval-rag", help="recall@5 / MRR@5: vector vs full-text vs hybrid")
    er.add_argument("--queries", default="evals/rag_queries.yaml")
    er.add_argument("--suggest", action="store_true",
                    help="propose candidates for unlabeled queries (you label them)")
    er.add_argument("--out", default="evals/results")
    er.set_defaults(func=cmd_eval_rag)

    ea = sub.add_parser("eval-assistant",
                        help="Score the assistant on fixed synthetic cases (OpenAI, ~cents)")
    ea.add_argument("--prompt-version", help="assistant prompt, e.g. a1 (default: config)")
    ea.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    ea.add_argument("--repeat", type=int, default=1, help="run each case N times (variance)")
    ea.add_argument("--baseline", help="earlier result file to compare with")
    ea.add_argument("--rescore", help="re-score a saved result file with current scorers (no LLM)")
    ea.add_argument("--limit", type=int, help="only the first N cases")
    ea.add_argument("--database", default="yti_eval", help="fixed eval database")
    ea.add_argument("--cases", default="evals/assistant_cases.yaml")
    ea.add_argument("--seed", default="evals/fixtures/assistant_seed.sql")
    ea.add_argument("--no-reseed", action="store_true", help="keep the current eval data")
    ea.add_argument("--out", default="evals/results")
    ea.set_defaults(func=cmd_eval_assistant)

    ls = sub.add_parser("llm-stats", help="LLM usage: calls, tokens, cost, latency (yti.v_llm_daily)")
    ls.add_argument("--days", type=int, default=7, help="number of days to show (default 7)")
    ls.set_defaults(func=cmd_llm_stats)

    rt = sub.add_parser("retention", help="Dọn dữ liệu quá hạn (MẶC ĐỊNH chỉ xem trước)")
    rt.add_argument("--apply", action="store_true",
                    help="XÓA THẬT. Không có cờ này thì chỉ liệt kê, không xóa gì.")
    rt.set_defaults(func=cmd_retention)

    v = sub.add_parser("verify-channels", help="Xác minh channel_id bằng API (~1 unit/kênh)")
    v.set_defaults(func=cmd_verify_channels)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, as_json=args.json_logs)
    try:
        return args.func(args)

    except ConfigError as e:
        # Mã 2 RIÊNG cho lỗi cấu hình: Airflow/CI biết retry là vô ích,
        # cần con người sửa. Gộp chung mã 1 thì hệ thống sẽ retry 3 lần rồi
        # mới bỏ cuộc - lãng phí và làm chậm việc phát hiện.
        print(f"LỖI CẤU HÌNH: {e}", file=sys.stderr)
        return 2

    except Exception as e:                                  # noqa: BLE001
        # ⭐ TRACEBACK LÀ CHO LẬP TRÌNH VIÊN; THÔNG BÁO LÀ CHO NGƯỜI VẬN HÀNH.
        # Hạ tầng sập mà ném 30 dòng traceback của botocore thì không ai đọc
        # được, và nó KHÔNG NÓI PHẢI LÀM GÌ. CLI dùng được lúc 2 giờ sáng phải
        # nói rõ: hỏng ở đâu, và bước tiếp theo là gì.
        # Traceback đầy đủ vẫn được ghi qua logger khi bật -v.
        hint = _diagnose(e)
        print(f"\nLỖI: {type(e).__name__}", file=sys.stderr)
        print(f"  {str(e)[:300]}", file=sys.stderr)
        if hint:
            print(f"\n  → {hint}", file=sys.stderr)
        print("\n  (chạy lại với -v để xem traceback đầy đủ)", file=sys.stderr)
        logging.getLogger(__name__).debug("Chi tiết:", exc_info=True)
        return 3                                            # 3 = lỗi hạ tầng

    except KeyboardInterrupt:
        print("\nĐã hủy.", file=sys.stderr)
        return 130          # quy ước Unix: 128 + SIGINT(2)


def _diagnose(exc: Exception) -> str | None:
    """Đoán nguyên nhân và GỢI Ý HÀNH ĐỘNG từ thông báo lỗi.

    Đây là "runbook nhúng trong code": thay vì bắt người vận hành đi tra tài
    liệu, chương trình tự nói ra việc cần làm. Rẻ để viết, cứu rất nhiều thời
    gian khi sự cố - nhất là với người không xây hệ thống này.
    """
    text = str(exc).lower()
    if "9000" in text or "minio" in text or "endpoint url" in text:
        return ("Không kết nối được MinIO. Kiểm tra: docker compose ps  |  "
                "chạy ngoài Docker thì MINIO_ENDPOINT phải là http://localhost:9000")
    if "5432" in text or "could not connect to server" in text or "connection refused" in text:
        return ("Không kết nối được Postgres. Kiểm tra: docker compose ps  |  "
                "chạy ngoài Docker thì POSTGRES_CONN_HOST phải là localhost")
    if "docker daemon" in text:
        return "Docker chưa chạy: sudo systemctl enable --now docker && docker context use default"
    return None


if __name__ == "__main__":
    sys.exit(main())
