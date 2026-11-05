import json
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit
from collections import deque
from .models import SearchConfig, Job, Candidate, now
from .search import CodexCandidateProvider, UnconfiguredSearchProvider
from .network import Fetcher, FetchError
from .robots_cache import RobotsCache
from .parsers import parse_html, ParseError, links, enrich
from .matching import deduplicate, evaluate
from .exporter import export_bundle
from .catalog import read_rows
from .planning import cities, validate_receipt, fingerprint, ensure_new_execution, followup_plan
from .entry_reader import SharedReader, read_entry_steps
from .navigation import verify_navigation, canonical, discover_career
from .page_reader import extract_page_steps
from .traversal import scope_id, initial_state
from .company_scope import scope_evidence
from .resources import ResourceStore, record_candidates, record_task_companies, name_key
from .announcements import verify_announcement_employer, record_profile, read_announcement
from .execution_audit import build_execution_audit, is_content_event

from .channels import access_mode as channel_access_mode, label as channel_label, CHANNELS as CHANNEL_SPECS
from . import access_control
from .paths import WORKSPACE as ROOT


def local_path(path, base=ROOT):
    p = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError("本阶段只读取项目内显式提供的文件，请先将有权使用的材料复制到项目")
    return p


def output_path(path, mode):
    p = local_path(path)
    expected = ROOT / "outputs" / mode
    if not p.is_relative_to(expected) or p == expected:
        raise ValueError(f"输出必须位于 outputs/{mode}/<独立运行目录>，不得混用模拟与真实结果")
    if p.exists() and any(p.iterdir()):
        raise ValueError("输出目录不是空目录，请选择新运行目录")
    return p


def load_config(path):
    from .spec import matching_config
    return matching_config(json.loads(local_path(path).read_text(encoding="utf-8-sig")))


def page_cache_key(candidate):
    """页面级缓存键：来源渠道 + 岗位/页面身份 + 查询上下文 + 账号范围。

    换城市、换关键词、换账号都不会命中同一份缓存，避免把别的范围的结果复用过来。
    """
    return access_control.cache_key(
        candidate.source_channel,
        canonical(candidate.url),
        query_context={"query": candidate.query,
                       "cities": sorted(candidate.target_cities or []),
                       "company": candidate.company},
        account_scope=("browser_session" if channel_access_mode(candidate.source_channel)
                       != "http_listing" else "anonymous"),
    )


def reuse_cached_page(candidate, workspace):
    """优先复用已读取并结构化解析过的页面，避免重复打开与重复调用模型。"""
    if candidate.local_file:
        return None
    try:
        entry = access_control.cache_get(workspace, page_cache_key(candidate))
    except (OSError, ValueError):
        return None
    if not entry or not entry.get("jobs"):
        return None
    if entry.get("content_hash") and not entry.get("retrieved_at"):
        return None
    return {
        "jobs": [Job.model_validate(row) for row in entry["jobs"]],
        "retrieved_at": entry.get("retrieved_at"),
        "reason": "复用已读取的结构化页面结果；证据时间保持原读取时间，不冒充本次重新读取",
    }


def store_cached_page(candidate, jobs, workspace, retrieved_at=None):
    """保存页面级结构化解析结果，供恢复或同范围重跑时复用。"""
    if candidate.local_file or not jobs:
        return None
    payload = {
        "schema_version": "page-cache-1",
        "channel": candidate.source_channel,
        "url": candidate.url,
        "query_context": {"query": candidate.query,
                          "cities": sorted(candidate.target_cities or [])},
        "retrieved_at": retrieved_at or now(),
        "jobs": [job.model_dump(mode="json") for job in jobs],
    }
    try:
        return access_control.cache_put(workspace, page_cache_key(candidate), payload)
    except OSError:
        return None


def import_jobs(candidate):
    if not candidate.authorized_import:
        raise ValueError("人工文件未声明有权提供")
    path = local_path(candidate.local_file)
    data = path.read_bytes()
    if len(data)>10000000:
        raise ValueError("导入文件大于10MB，需要另行审核")
    if path.suffix.lower() in (".html", ".htm"):
        return parse_html(data,candidate,str(path))
    if path.suffix.lower() in (".json", ".csv", ".xlsx"):
        records = json.loads(data.decode("utf-8-sig")) if path.suffix.lower()==".json" else read_rows(path)
        output=[]
        for record in records:
            record.setdefault("source_url",candidate.url)
            record["source_channel"]="manual_import"
            record["synthetic"]=candidate.synthetic
            record.pop("evidence",None)
            job=Job.model_validate(record)
            job.notes.append("用户有权提供的人工导入材料；不代表自动读取原站成功")
            output.append(enrich(job,data,"authorized_manual_import",str(path)))
        return output
    raise ParseError("附件类型尚未实现可靠岗位拆分；PDF文本/扫描件/图片须人工处理，不能记为解析成功")


def run(config, output, candidates_file=None, policies_file=None, mode="real", catalog_file=None, operation="provided_links", plan_file=None, effective=None, resource_path=None, resume_file=None, resume_budget='remaining', run_id=None):
    output=output_path(output,mode)
    provider=CodexCandidateProvider(local_path(candidates_file)) if candidates_file else UnconfiguredSearchProvider()
    candidates, discovery=provider.discover(config)
    plan = {}
    resume_states, prior_jobs = {}, []
    if resume_file:
        checkpoint=json.loads(local_path(resume_file).read_text(encoding='utf-8-sig'))
        if checkpoint.get('schema_version')!='traversal-run-1' or checkpoint.get('config_fingerprint')!=fingerprint(config):
            raise ValueError('遍历进度与当前配置/规则不一致，不能混用')
        if resume_budget not in ('remaining','new_run'):
            raise ValueError('恢复预算只能为remaining或new_run')
        resume_states={key:value for key,value in checkpoint['states'].items() if not value['complete']}
        candidates=[Candidate.model_validate(state['candidate']) for state in resume_states.values()]
        from .traversal import job_identity
        replacing={job_identity(Job.model_validate(j)) for state in resume_states.values() for j in state['jobs']}
        prior_jobs=[Job.model_validate(j) for j in checkpoint['jobs'] if job_identity(Job.model_validate(j)) not in replacing]
        plan={'budget_used':checkpoint['budget_used'] if resume_budget=='remaining' else {},
              'query_history':checkpoint.get('query_history',[]) if resume_budget=='remaining' else []}
        discovery.update(status='resume_traversal',resume_budget_mode=resume_budget,prior_progress=str(resume_file),
                         coverage='只继续未完成遍历；已读原文证据沿用原时间，不冒充本次重新读取')
    run_state = None
    if operation == "new_search":
        if not candidates_file or not plan_file:
            raise ValueError("new-search需要本次计划与实际搜索凭据；Python不能调用宿主 agent 的会话搜索")
        receipt=json.loads(local_path(candidates_file).read_text(encoding="utf-8-sig"))
        plan=json.loads(local_path(plan_file).read_text(encoding="utf-8-sig"))
        from . import workflow
        run_state=workflow.load_state(ROOT,run_id) if run_id else None
        if run_state is not None and run_state.get('plan_id') not in (None,plan['plan_id']):
            raise ValueError('运行状态与本次计划不一致，不能把两次运行合并')
        discovery["search_log"]=validate_receipt(config,plan,receipt,
                                                 run_protocol=(run_state or {}).get('receipt_protocol'))
        # 分批提交：按 run_id + task_id + attempt_id/receipt_id 去重，不同批次可连续累计。
        if run_state is not None:
            # 先判重（已经是本轮的同一批执行就拒绝重复入库），再登记本批，
            # 这样第二批新任务不会因为第一批导出过而被整体拒收。
            ensure_new_execution(plan,discovery['search_log'],ROOT/'outputs'/'real',state=run_state,run_id=run_id)
            accepted,duplicates=workflow.claim_receipts(run_state,discovery['search_log'])
            if duplicates:
                discovery['duplicate_receipts']=duplicates
            if accepted:
                workflow.merge_receipt(run_state,receipt,config,plan)
            workflow.save_state(run_state,ROOT)
            discovery['run_id']=run_id
        else:
            ensure_new_execution(plan,discovery['search_log'],ROOT/'outputs'/'real')
        discovery['entry_tasks'] = plan.get('entry_tasks',[])
        for task in plan.get('entry_tasks',[]):
            if task['status'] == 'planned':
                candidates.append(Candidate.model_validate(task['candidate']))
        discovery.update(status="new_queries_executed" if any(x.get("status")=="success" for x in discovery["search_log"]) else "search_incomplete",plan_id=plan["plan_id"],search_mode=receipt.get("search_mode","not_exposed"))
    else:
        discovery["historical_search_log"]=discovery.pop("search_log",[])
        discovery["search_log"]=[]
        if operation == "refresh":
            discovery.update(status="refresh_known_candidates",coverage="重新读取已知候选；未执行新搜索，未扩大覆盖")
    discovery.update(operation=operation,config_fingerprint=fingerprint(config),effective_config=effective,live_refetched=False)
    discovery['input_artifacts'] = {label:dict(path=str(local_path(path)),sha256=hashlib.sha256(local_path(path).read_bytes()).hexdigest())
        for label,path in [('candidates',candidates_file),('plan',plan_file),('policies',policies_file)] if path}
    discovery["candidates"]=[c.model_dump(mode="json") for c in candidates]
    if mode=="real" and any(c.synthetic for c in candidates):
        raise ValueError("合成候选不能进入真实运行")
    if mode=="test" and any(not c.synthetic for c in candidates):
        raise ValueError("离线样例只允许明确标记的合成候选")
    store=ResourceStore(resource_path) if resource_path is not None and mode=="real" else None
    if store:
        resource_ids=record_candidates(store,candidates)
        resource_ids=list(dict.fromkeys(resource_ids+record_task_companies(store,discovery.get('search_log',[]))))
        store.snapshot()
        discovery['company_library']={'path':str(store.path),'discovered_company_ids':resource_ids}
    policies=json.loads(local_path(policies_file).read_text(encoding="utf-8")) if policies_file else {}
    targets=cities(config)
    prior_budget = plan.get('budget_used',{})
    robots_cache = RobotsCache(Path(resource_path).parent/'network-cache' if resource_path is not None and mode=='real' else None)
    fetchers={c:Fetcher(config.budget.model_copy(update={"requests":config.budget.city_requests(i,len(targets),prior_budget.get('requests',{}).get(c,0))}),policies,output/"evidence") for i,c in enumerate(targets)}
    shared_last={}
    shared_intervals={}
    stopped_hosts=dict(checkpoint.get('stopped_hosts',{})) if resume_file and resume_budget=='remaining' else {}
    for client in fetchers.values():
        client.last=shared_last
        client.stopped_hosts=stopped_hosts
        client.robots_cache=robots_cache
        client.required_intervals=shared_intervals
    company_sets={c:{name_key(n):n for n in prior_budget.get('companies',{}).get(c,[])} for c in targets}
    read_memo, fallback_requests = {}, []
    jobs=list(prior_jobs)
    reports=[]
    companies=[]
    if catalog_file:
        companies=json.loads(local_path(catalog_file).read_text(encoding="utf-8"))["companies"]
    seen_channels=set()
    traversals=dict(checkpoint['states']) if resume_file else {}
    visited_entries=set(checkpoint.get('visited_entries',[])) if resume_file else set()
    for candidate in candidates:
        if not candidate.local_file and candidate.source_channel in config.sources and channel_access_mode(candidate.source_channel)=='http_listing' and candidate.candidate_type!='enterprise_directory' and (not candidate.target_cities or set(candidate.target_cities).intersection(targets)):
            traversals.setdefault(scope_id(candidate),initial_state(candidate,'entry_not_started'))

    def usage():
        return dict(requests={c:prior_budget.get('requests',{}).get(c,0)+f.requests for c,f in fetchers.items()},
                    searches={c:prior_budget.get('searches',{}).get(c,0)+sum(q.get('target_city')==c and q.get('status') in ('success','failed','restricted') for q in discovery.get('search_log',[])) for c in targets},
                    companies={c:sorted(v.values()) for c,v in company_sets.items()},
                    company_counts={c:len(v) for c,v in company_sets.items()})

    def save_progress(stage):
        for r in reports:
            if r.get('resolved_home_scope_id'):
                traversals.pop(r['resolved_home_scope_id'],None)
            for pending in r.get('entry_pending_states',[]):
                traversals.setdefault(pending['scope_id'],pending)
        traversals.update({r['traversal']['scope_id']:r['traversal'] for r in reports if r.get('traversal')})
        discovery['budget_used']=usage()
        output.mkdir(parents=True,exist_ok=True)
        attempts=[a | {'budget_city':c} for c,f in fetchers.items() for a in f.read_attempts]
        events=[e | {'budget_city':c} for c,f in fetchers.items() for e in f.events]
        audit,processing,continuations=build_execution_audit(candidates,reports,traversals,attempts,events,discovery,
            str(output/'traversal-progress.json') if traversals else None)
        audit['stage']=stage
        discovery.update(core_execution=audit,company_processing=processing,continuation_requests=continuations,
                         read_attempts=attempts)
        for name,value in [('core-execution.json',audit),('company-processing.json',processing),('continuation-requests.json',continuations)]:
            temporary=output/(name+'.tmp')
            temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
            temporary.replace(output/name)
        (output/'read-attempts.jsonl').write_text(''.join(json.dumps(a,ensure_ascii=False)+'\n' for a in attempts),encoding='utf-8')
        if not traversals:
            return
        saved_jobs=deduplicate([Job.model_validate(j) for s in traversals.values() for j in s['jobs']]+jobs)
        progress=dict(schema_version='traversal-run-1',config_fingerprint=fingerprint(config),
            states=traversals,budget_used=usage(),jobs=[j.model_dump(mode='json') for j in saved_jobs],
            updated_at=now(),stage=stage,complete=all(s['complete'] for s in traversals.values()),
            visited_entries=sorted(visited_entries),stopped_hosts=dict(stopped_hosts),
            query_history=list(dict.fromkeys(plan.get('query_history',[])+[q['query'] for q in discovery.get('search_log',[]) if q.get('status') in ('success','failed','restricted')])),
            network_events=(checkpoint.get('network_events',[]) if resume_file else [])+[e | {'budget_city':c} for c,f in fetchers.items() for e in f.events],
            search_log=(checkpoint.get('search_log',[]) if resume_file else [])+discovery.get('search_log',[]),
            resume_budget_mode=resume_budget if resume_file else 'initial')
        from .spec import spec_for_config
        progress.update(rules_version=config.rules_version,effective_search_spec=spec_for_config(config).model_dump(mode='json'),raw_input=(effective or {}).get('raw_input',config._raw_input))
        discovery['traversal_progress']=progress
        output.mkdir(parents=True,exist_ok=True)
        temporary=output/'traversal-progress.json.tmp'
        temporary.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(output/'traversal-progress.json')

    def process_candidate(candidate):
        employer_pages=[]
        owner=next((c for c in candidate.target_cities if c in targets),targets[0])
        fetcher=SharedReader(fetchers[owner],read_memo)
        seen_channels.add(candidate.source_channel)
        report={"channel":candidate.source_channel,"url":candidate.url,"access_status":"not_run","parse_status":"not_run","jobs_extracted":None,"reason":""}
        checklist={"legal_name":candidate.company,"official_url":candidate.official_url,"career_url":candidate.career_url,"source_url":candidate.url,"version_date":candidate.discovered_at[:10],"discovery_kind":"unknown","verification_status":"pending","notes":"搜索发现的主体/网址仍需原文与导航核实；不凭标题认定官方"}
        report.update(company=candidate.company,target_cities=candidate.target_cities,task_ids=candidate.task_ids,entry_task_id=candidate.entry_task_id,candidate_type=candidate.candidate_type,attempted=False)
        reports.append(report)
        cached = reuse_cached_page(candidate, ROOT)
        if cached:
            jobs.extend(cached["jobs"])
            report.update(access_status="cached_progress", parse_status="success",
                          jobs_extracted=len(cached["jobs"]), attempted=False,
                          coverage_incomplete=False, reason=cached["reason"],
                          content_read_at=cached.get("retrieved_at"))
            if candidate.company and not any(c.get("legal_name") == candidate.company
                                             for c in companies):
                companies.append(checklist)
            return
        try:
            if candidate.candidate_type == 'enterprise_directory':
                raise FetchError('not_run','企业目录只用于企业发现；通过import-directory/import-companies导入，不作为岗位详情读取')
            if candidate.target_cities and not set(candidate.target_cities).intersection(targets):
                raise FetchError("not_run","候选发现城市与本次目标不交集，未检查不等于无岗位")
            if candidate.company:
                if name_key(candidate.company) not in company_sets[owner] and config.budget.companies_per_city is not None and len(company_sets[owner]) >= config.budget.companies_per_city:
                    raise FetchError("budget_exhausted","本城市企业检查预算耗尽")
                company_sets[owner].setdefault(name_key(candidate.company),candidate.company)
            if candidate.source_channel not in config.sources:
                raise FetchError("not_run","渠道未启用")
            if candidate.local_file:
                if candidate.synthetic != (mode=="test"):
                    raise ValueError("导入数据模式不匹配")
                extracted=import_jobs(candidate)
                report.update(access_status="manual_import",parse_status="success")
                checklist["verification_status"]="manual_import_not_online_verified"
            else:
                if mode=="test":
                    raise ValueError("测试模式禁止联网")
                if channel_access_mode(candidate.source_channel) != "http_listing":
                    raise FetchError("requires_authorization",
                        f"{channel_label(candidate.source_channel)}没有可用的授权自动接口，"
                        "只支持候选链接记录、本机可见浏览器读取和用户有权提供的材料")
                if candidate.source_channel == 'official_career':
                    candidate,page,employer_pages,extracted = yield from read_entry_steps(fetcher,candidate,config,store,reports,report,resume_states,visited_entries)
                    checklist.update(verification_status='source_classified_no_identity_gate',
                                     career_url=report.get('resolved_career_url') or candidate.career_url,
                                     notes=json.dumps(report.get('entry_trace',[]),ensure_ascii=False))
                    if report.get('fallback_search_reason') and candidate.company:
                        for city in report.get('fallback_target_cities',candidate.target_cities or [owner]):
                            if city in targets:
                                fallback_requests.append(dict(company=candidate.company,target_city=city,
                                    source_url=candidate.url,reason=report['fallback_search_reason']))
                elif resume_file and scope_id(candidate) in resume_states and not resume_states[scope_id(candidate)].get('entry_pending'):
                    page=None
                    extracted=yield from extract_page_steps(fetcher,candidate,None,reports,report,targets=targets,config=config,state=resume_states[scope_id(candidate)])
                else:
                    public_notice=candidate.source_channel=='public_recruitment'
                    publisher_policy=policies.get(urlsplit(candidate.url).hostname,{})
                    if public_notice and not (publisher_policy.get('publisher_verified') and publisher_policy.get('publisher_evidence_url')):
                        raise FetchError('policy_pending','公开招聘转载来源发布方尚未审核；不当作企业官网')
                    if not public_notice and not candidate.official_url:
                        raise FetchError("official_unverified","缺少官网来源，不能认定官方入口")
                    report["attempted"]=True
                    if candidate.announcement_url and not public_notice:
                        report['announcement_evidence']=verify_announcement_employer(fetcher,candidate,store)
                    if public_notice and candidate.candidate_type=='announcement' and store:
                        notice_companies,home=read_announcement(fetcher,candidate.url,store)
                        report['discovered_employers']=notice_companies
                    else:
                        home=fetcher.get(candidate.url if public_notice else candidate.official_url)
                    if not public_notice and candidate.company:
                        identity=record_profile(store,candidate.company,home.get('final_url',candidate.official_url),home,candidate.official_url)
                        report['company_identity_status']='name_on_original_home' if identity else 'pending'
                        if candidate.announcement_url and not identity:
                            raise FetchError('employer_unverified','公告企业全称未在官网原文核实；品牌与法律主体关系仍待确认')
                    employer_pages.append((candidate.url if public_notice else candidate.official_url,home))
                    home_links=links(home["data"],home.get("final_url",candidate.url if public_notice else candidate.official_url))
                    if not public_notice and not candidate.career_url:
                        choices=discover_career(fetcher,candidate,home)
                        report['discovered_links']=[x | {'status':'entry_available' if x['approved'] else 'policy_pending','parent_url':candidate.official_url} for x in choices]
                        approved=[x for x in choices if x['approved']]
                        if not approved:
                            raise FetchError('policy_pending' if choices else 'navigation_missing','官网已读取；招聘导航待审核' if choices else '官网已读取，但未找到明确招聘导航；不代表无岗位')
                        selected=approved[0]['url']
                        candidate=candidate.model_copy(update={'career_url':selected,'url':selected if candidate.candidate_type=='company_home' else candidate.url,'candidate_type':'career_entry' if candidate.candidate_type=='company_home' else candidate.candidate_type,'original_discovered_url':candidate.url})
                        checklist['career_url']=selected
                        report['resolved_career_url']=selected
                    if candidate.company_profile_url:
                        if not any(canonical(a['url'])==canonical(candidate.company_profile_url) for a in home_links):
                            raise FetchError('official_unverified','企业简介页未在已读取官网导航中明确关联')
                        profile_page=fetcher.get(candidate.company_profile_url)
                        employer_pages.append((candidate.company_profile_url,profile_page))
                        if not public_notice and candidate.company:
                            record_profile(store,candidate.company,candidate.company_profile_url,profile_page)
                    nav={'publisher_evidence':publisher_policy.get('publisher_evidence_url'),'employer_official':False} if public_notice else verify_navigation(fetcher,candidate,home)
                    if store and not public_notice and candidate.company and report.get('company_identity_status')=='name_on_original_home':
                        store.add(candidate.company,candidate.official_url,home['retrieved_at'],'official_navigation',
                                  values={'career_url':candidate.career_url},supported=('career_url',),
                                  evidence={'quote':json.dumps(nav,ensure_ascii=False),'content_hash':home['hash'],'snapshot':home['snapshot']})
                    careers=home if public_notice else fetcher.get(candidate.career_url)
                    if not public_notice and candidate.company:
                        record_profile(store,candidate.company,candidate.career_url,careers)
                    career_links=links(careers["data"],candidate.url if public_notice else candidate.career_url)
                    detail_link=[a for a in career_links if canonical(a["url"])==canonical(candidate.url)]
                    if public_notice or canonical(candidate.url)==canonical(candidate.career_url):
                        detail_link=[{"url":candidate.url,"status":"入口内含公告；仍须逐岗位提取"}]
                    page=fetcher.get(candidate.url)
                    employer_pages.append((candidate.url,page))
                    if not detail_link:
                        # A surviving first-party archived detail may no longer be listed.
                        # Require its own backlink to the already verified career navigation.
                        # This proves origin, NOT that the vacancy is still open.
                        cp, dp = urlsplit(candidate.career_url), urlsplit(candidate.url)
                        backlinks=[a for a in links(page["data"],candidate.url) if a["url"].rstrip("/")==candidate.career_url.rstrip("/")]
                        if cp.hostname!=dp.hostname or not dp.path.startswith(cp.path.rstrip("/")+"/") or not backlinks:
                            raise FetchError("official_unverified","招聘入口与候选详情没有可核实的导航/同站路径反向关联")
                        detail_link=[{"reverse_link":backlinks,"status":"原站历史详情；当前列表未确认，开放状态未知"}]
                    checklist.update(verification_status="public_publisher_not_employer_verified" if public_notice else "official_navigation_verified",notes=json.dumps({"home":candidate.official_url,"home_hash":home["hash"],"navigation":nav,"career_hash":careers["hash"],"detail_link":detail_link},ensure_ascii=False))
                    if not public_notice and candidate.company:
                        record_profile(store,candidate.company,candidate.url,page)
                    report["access_status"]="success"
                    report.update(content_read_at=page["retrieved_at"],validity_checked_at=page["retrieved_at"])
                    if candidate.candidate_type=="company_home":
                        raise FetchError("not_run","企业主页不能作为具体岗位")
                    extracted=yield from extract_page_steps(fetcher,candidate,page,reports,report,targets=targets,config=config)
            for j in extracted:
                if j.source_type == 'unknown' and candidate.source_channel != 'official_career':
                    from .entries import source_type
                    j.source_type = source_type(candidate)
                if candidate.source_channel=='public_recruitment':
                    j.notes.append('公开招聘发布方原文，非企业官网；招聘主体与当前岗位有效性需复核')
                for source_url, employer_page in employer_pages:
                    category_evidence=scope_evidence(employer_page,source_url,j.company,config.company_scope_keywords)
                    if category_evidence:
                        j.evidence.append(category_evidence)
                j.discovery_task_ids=list(dict.fromkeys(j.discovery_task_ids+candidate.task_ids))
                j.discovery_cities=list(dict.fromkeys(j.discovery_cities+candidate.target_cities))
                if not candidate.local_file:
                    j.validity_checked_at=j.validity_checked_at or (page["retrieved_at"] if page else None)
                if candidate.company and j.company!=candidate.company:
                    j.notes.append("候选企业名称与原始招聘主体名称不同，以原文为准，目录关联待确认")
            if store:
                for j in extracted:
                    store.add(j.company,j.source_url,j.evidence[0].retrieved_at if j.evidence else now(),'job_original',
                              context={'title':j.title,'source_channel':j.source_channel,'candidate_company':candidate.company})
                    if j.evidence and not j.synthetic:
                        evidence = next((e for e in j.evidence if e.method not in ('search_snippet','fixture')), None)
                        if evidence and evidence.field_quotes.get('company') == j.company and evidence.field_quotes.get('title') == j.title:
                            work_cities = list(dict.fromkeys(r.geo.city for r in evaluate(j,config) if r.geo.state=='PASS' and r.geo.city))
                            store.add(j.company,evidence.source_url,evidence.retrieved_at,'job_original',
                                      values={'hiring_history':[{'title':j.title,'cities':work_cities}]},supported=('hiring_history',),
                                      evidence={'quote':json.dumps(evidence.field_quotes,ensure_ascii=False),
                                                'content_hash':evidence.content_hash,'entity_name':j.company,
                                                'kind':'job','subject_relation':'self'})
            jobs.extend(extracted)
            if report.get('access_status') == 'success' and extracted:
                store_cached_page(candidate, extracted, ROOT, report.get('content_read_at'))
            if report.get('parse_status') == 'success' and not report.get('adapter') and not report.get('entry_trace'):
                report.update(jobs_extracted=len(extracted),reason="原始内容已提取；岗位仍开放及求职条件另行判断")
        except FetchError as exc:
            report.update(access_status=exc.status,reason=str(exc))
            if exc.status=='budget_exhausted' and candidate.source_channel=='official_career' and not candidate.local_file and not report.get('traversal'):
                report.update(traversal=initial_state(candidate,exc.status),coverage_incomplete=True)
        except (ParseError, ValueError, OSError) as exc:
            report.update(parse_status="failed",reason=str(exc))
        if candidate.company and not any(c.get("legal_name")==candidate.company for c in companies):
            companies.append(checklist)
        else:
            for c in companies:
                if c.get("legal_name")==candidate.company:
                    c.update({k:v for k,v in checklist.items() if k in ("verification_status","notes")})
    lanes={c:deque() for c in targets}
    scheduled=set()
    for candidate in candidates:
        key=scope_id(candidate)
        if key in scheduled:
            continue
        scheduled.add(key)
        owner=next((c for c in candidate.target_cities if c in targets),targets[0])
        lanes[owner].append(process_candidate(candidate))
    save_progress('started')
    try:
        while any(lanes.values()):
            for city in targets:
                if not lanes[city]:
                    continue
                step=lanes[city].popleft()
                try:
                    next(step)
                except StopIteration:
                    pass
                else:
                    lanes[city].append(step)
                save_progress('reading')
    except BaseException:
        save_progress('interrupted')
        raise
    save_progress('finished')
    for spec in CHANNEL_SPECS:
        if spec.name in seen_channels or spec.stage in ("none",):
            continue
        if channel_access_mode(spec.name) == "http_listing":
            continue
        reason = ("平台禁止未授权自动化：默认人工辅助，用户提供岗位原文后可入库匹配"
                  if spec.name == "linkedin" else
                  "无已配置的授权自动检索；可记录候选和人工导入"
                  if spec.name == "boss" else
                  "本轮未取得逐站点自动访问依据；不因一次失败认定永久不可用"
                  if spec.name == "bendibao_web" else
                  "公众号自动获取未接入；可有权导入")
        reports.append(dict(channel=spec.name,url=None,access_status="not_run",
                            parse_status="not_run",jobs_extracted=None,reason=reason))
    if not candidates:
        reports.append(dict(channel="search",url=None,access_status=discovery["status"],parse_status="not_run",jobs_extracted=None,reason=discovery["coverage"]))
    jobs=deduplicate(jobs)
    results=[r for j in jobs for r in evaluate(j,config)]
    events=[e | {"budget_city":c} for c,f in fetchers.items() for e in f.events]
    discovery["live_refetched"]=any(e.get('status')=='success' and is_content_event(e) for e in events)
    discovery["budget_allocation"]={c:f.budget.requests for c,f in fetchers.items()}
    if mode == 'real' and fallback_requests:
        discovery['follow_up_plan'] = followup_plan(config,fallback_requests,discovery.get('search_log',[]),plan,usage())
    discovery['entry_log'] = []
    for task in discovery.get('entry_tasks',[]):
        checked = next((r for r in reports if r.get('entry_task_id') == task['task_id']),{})
        discovery['entry_log'].append(dict(task_id=task['task_id'],company=task['company'],target_city=task['target_city'],
            status=checked.get('entry_outcome',checked.get('access_status',task['status'])),
            reason=checked.get('fallback_search_reason') or checked.get('reason') or task['reason']))
    # 整轮完成判定统一交给 workflow：所有入口共用同一 finalizer，不再各自解释"成功"。
    # 旧字段 execution_status 继续保留，但只作为 completion 的投影。
    # 注意：渠道级报告（url=None）也必须参与缺口判定，不能因为拿不到详情页就隐身。
    evidence_gap = any(r.get("coverage_incomplete")
                       or r.get("access_status") not in ("success", "manual_import", "cached_progress")
                       or r.get("parse_status") not in ('success', 'listing', 'navigation')
                       or any(x.get('status') in ('policy_pending', 'budget_exhausted') for x in r.get('discovered_links', []))
                       for r in reports) \
        or any(q.get("status") != "success" for q in discovery.get("search_log", [])) \
        or any(t['status'] != 'relevant_job_found' for t in discovery['entry_log'])
    from . import workflow
    if run_state is not None:
        completion = workflow.finalize(run_state, persist=True, workspace=ROOT)
    else:
        completion = workflow.finalize(workflow.state_from_plan_logs(plan, discovery.get('search_log', []), config, reports))
    completion = workflow.completion_with_channels(completion, config, reports)
    if evidence_gap and completion["verdict"] == "COMPLETE":
        completion["verdict"] = "PARTIAL"
        completion["partial_export"] = True
        completion["reasons"].append("存在页面读取或解析缺口（含未读取的渠道级记录）")
    discovery["completion"] = completion
    discovery["run_id"] = completion["run_id"]
    discovery["execution_status"] = ("completed_within_declared_scope"
                                     if completion["verdict"] == "COMPLETE" else "partial")
    if operation == "new_search":
        discovery["status"] = {"COMPLETE": "completed_within_declared_scope",
                               "WAITING_USER": "incomplete_pending",
                               "IN_PROGRESS": "incomplete_pending",
                               "FAILED": "search_failed"}.get(completion["verdict"], "search_partial")
    for record in discovery["candidates"]:
        report=next((r for r in reports if r.get("url")==record["url"]),None)
        record["processing_status"]=f"{report['access_status']}/{report['parse_status']}" if report else "not_run"
    if store:
        snapshot=store.snapshot()
        discovery['company_library']['total_companies']=len(snapshot['companies'])
    return export_bundle(output,config,jobs,results,reports,companies,discovery,events,mode)


def run_linkedin_import(config, material_path, output, effective=None, mode="real"):
    """领英人工辅助路径：只处理用户提供的岗位原文，不做自动抓取。

    登录/会话由本机独立浏览器完成，用户在领英站内自己搜索；这里只把用户交来的
    原文变成带证据的岗位，走同一套 evaluate/export。来源固定 `linkedin`/`platform`。
    """
    from . import linkedin, workflow
    output = output_path(output, mode)
    source = local_path(material_path)
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    profile = linkedin.access_profile(payload.get("access_mode") or linkedin.DEFAULT_MODE,
                                      payload.get("license_evidence"))
    jobs, problems = linkedin.material_jobs(payload, mode=profile["mode"], source_ref=str(source))
    jobs = deduplicate(jobs)
    results = [r for j in jobs for r in evaluate(j, config)]
    reports = [dict(channel="linkedin", url=None,
                    access_status="user_provided_material" if jobs else "not_run",
                    parse_status="success" if jobs else "failed", jobs_extracted=len(jobs),
                    reason=("用户本人提供的领英岗位原文；未自动抓取、未自动投递"
                            if jobs else "没有可用的岗位原文"),
                    problems=problems)]
    discovery = {
        "provider": "user_provided_material",
        "status": "imported_from_user_material",
        "operation": "linkedin_assisted_import",
        "coverage": "仅覆盖用户提供的岗位原文；不代表领英站内全部岗位",
        "live_refetched": False,
        "linkedin": profile,
        "search_log": [],
        "candidates": [{"company": j.company, "title": j.title, "url": j.source_url,
                        "candidate_type": "job_detail", "target_cities": config.target_cities,
                        "source_channel": "linkedin"} for j in jobs],
        "effective_config": effective,
        "material_problems": problems,
    }
    state = workflow.state_from_plan_logs({"plan_id": "linkedin-import-" + hashlib.sha256(
        source.read_bytes()).hexdigest()[:8], "channels": ["linkedin"],
        "effective_config": {"budget": config.budget.model_dump(mode="json")}}, [], config, reports)
    state["scope"]["cities"] = list(config.target_cities)
    completion = workflow.finalize(state)
    completion["reasons"].append("领英渠道为人工辅助模式：覆盖仅限用户提供的岗位原文，不是站内全量搜索")
    completion["partial_export"] = True
    discovery["completion"] = completion
    discovery["run_id"] = completion["run_id"]
    return export_bundle(output, config, jobs, results, reports, [], discovery, events=[], mode=mode)


def _save_browser_snapshot(directory, snapshot, label):
    directory.mkdir(parents=True, exist_ok=True)
    stamp = str(snapshot.get("retrieved_at") or now()).replace(":", "").replace("+", "p")
    path = directory / f"{label}-{stamp[:19]}.json"
    payload = dict(snapshot)
    payload.setdefault("content_hash", hashlib.sha256(
        str(snapshot.get("text") or "").encode("utf-8")).hexdigest())
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot["snapshot_path"] = str(path)
    return path


def run_linkedin_browser_search(config, output, *, keywords, location, run_id=None,
                                max_details=None, workspace=ROOT, browser=None,
                                mode="real", user_confirmed=False, effective=None,
                                page_interval=None):
    """领英浏览器辅助检索：用用户本机已登录的浏览器读少量页面，取岗位原文。

    边界（写进代码，不只写文档）：
    - 必须由用户明确同意（`user_confirmed`），否则不驱动用户浏览器；
    - 只读 1 个站内搜索页 + 至多 `MAX_DETAILS` 条详情，不翻页、不批量；
    - 每次页面读取之间有最小间隔，并占用该渠道预算与 `detail_read` 计量；
    - 遇登录墙/验证码/安全检查立即停止，交回用户；
    - 不点击投递、不提交申请、不调内部接口。
    """
    from . import linkedin, workflow, access_control
    from .browser_backend import BrowserError, BrowserService
    from .geography import normalize_city

    if not user_confirmed:
        raise ValueError("浏览器辅助检索会驱动用户本机浏览器：必须由用户明确同意"
                         "（--user-confirmed）后才能执行")
    if not keywords or not str(keywords).strip():
        raise ValueError("缺少检索关键词")
    target_city = normalize_city(str(location or ""))
    if not target_city:
        raise ValueError("地点必须是已审核的大陆城市，不能留空或用模糊地区代替")
    if config.target_cities and target_city not in [normalize_city(c) for c in config.target_cities]:
        raise ValueError(f"检索地点 {target_city} 不在本轮目标城市 {config.target_cities} 内")
    limit = int(max_details or linkedin.DEFAULT_MAX_DETAILS)
    if not 1 <= limit <= linkedin.MAX_DETAILS:
        raise ValueError(f"详情页数量必须在 1–{linkedin.MAX_DETAILS} 之间（浏览器辅助是小样本）")
    interval = linkedin.MIN_PAGE_INTERVAL_SECONDS if page_interval is None else float(page_interval)

    output = output_path(output, mode)
    profile = linkedin.access_profile("browser_assisted")
    state = workflow.load_state(workspace, run_id) if run_id else None
    if state is not None:
        workflow.refresh_browser_state(state, workspace)
        status = workflow.auth_status(state, "linkedin")
        if status != "verified":
            raise ValueError(f"领英登录会话未核验（当前 {status}）：请先 auth-open / auth-probe，"
                             "浏览器辅助检索只在已核验的会话里执行")
        cooling = access_control.cooldown(workspace, "linkedin")
        if cooling:
            raise ValueError("领英当前处于" + cooling.get("state", "受限") + "状态：" +
                             str(cooling.get("reason") or "") + "；请等冷却结束或由用户显式重试")
        age = access_control.seconds_since_browser_action(workspace, "linkedin")
        if age is not None and age < interval:
            raise ValueError(f"距上次领英浏览器动作仅 {age:.1f} 秒，小于 {interval:.0f} 秒间隔；"
                             "请稍后再试，避免过快访问")

    service = browser or BrowserService(workspace)
    keywords_text = " ".join(keywords) if isinstance(keywords, (list, tuple)) else str(keywords)
    url = linkedin.search_url(keywords_text, target_city)
    jobs, problems, actions = [], [], []
    checkpoint = None
    login_required = None
    pages_read = 0
    tab_id = None
    lock = access_control.platform_lock(workspace, "linkedin")
    lock.acquire()
    try:
        try:
            auth_tab = ((state or {}).get("auth", {}).get("linkedin") or {}).get("tab_id")
            if auth_tab and getattr(service, "has_target", lambda _t: False)(auth_tab):
                handle = {"target_id": auth_tab, "session_id":
                          ((state or {}).get("auth", {}).get("linkedin") or {}).get("backend_session_id")}
                service.navigate(auth_tab, url)
            else:
                handle = service.open(url)
            tab_id = handle["target_id"]
            search_snapshot = service.snapshot(tab_id, wait_seconds=12)
            _save_browser_snapshot(output / "evidence", search_snapshot, "linkedin-search")
            pages_read += 1      # 页面确实打开过，如实计数；是否计费另按是否读到搜索结果
            linkedin.assert_not_blocked(search_snapshot)
            actions.append({"step": "search", "url": search_snapshot.get("url") or url})
            if state is not None:
                workflow.charge_access(state, channel="linkedin", city=target_city,
                                       count=1, note=f"领英站内搜索：{keywords_text}")
            access_control.note_browser_action(workspace, "linkedin", run_id=run_id,
                                               url=search_snapshot.get("url") or url)
            links = linkedin.extract_job_links(search_snapshot.get("anchors"), limit=limit)
        except linkedin.LinkedInCheckpoint as exc:
            checkpoint = str(exc)
            access_control.note_restricted(workspace, "linkedin", reason=str(exc),
                                           kind="captcha", run_id=run_id)
            links = []
        except linkedin.LinkedInLoginRequired as exc:
            # 未登录/会话失效：让该平台的登录态立即失效，任务保持 pending，等用户重新登录。
            login_required = str(exc)
            if state is not None:
                workflow.invalidate_auth(state, "linkedin", login_required)
            links = []
        except (BrowserError, OSError) as exc:
            raise ValueError(f"浏览器读取搜索页失败：{exc}") from exc

        for link in links:
            if checkpoint:
                break
            # 先看预算再开页面：额度不够时不要白白打开详情页（也减少无谓访问）。
            if state is not None:
                from . import budgets
                if budgets.channel_remaining(state, "linkedin", target_city) == 0 \
                        or budgets.kind_remaining(state, "detail_read", target_city) == 0:
                    problems.append({"job_id": link["job_id"], "url": link["url"],
                                     "reason": "预算不足，未继续读取详情（覆盖缺口，不代表没有岗位）"})
                    break
            if interval:
                time.sleep(interval)
            try:
                service.navigate(tab_id, link["url"])
                snapshot = service.snapshot(tab_id, wait_seconds=12)
                _save_browser_snapshot(output / "evidence", snapshot, f"linkedin-job-{link['job_id']}")
                pages_read += 1
                linkedin.assert_not_blocked(snapshot)
                if state is not None:
                    try:
                        workflow.charge_access(state, channel="linkedin", city=target_city,
                                               kind="detail_read", count=1,
                                               note=f"领英岗位详情：{link['job_id']}")
                    except ValueError as exc:
                        # 预算不足属于覆盖缺口：停在这里，不继续打开详情，也不伪装成"没有岗位"。
                        problems.append({"job_id": link["job_id"], "url": link["url"],
                                         "reason": "预算不足，未继续读取详情：" + str(exc)})
                        break
                actions.append({"step": "detail", "job_id": link["job_id"],
                                "url": snapshot.get("url") or link["url"]})
                access_control.note_browser_action(workspace, "linkedin", run_id=run_id,
                                                   url=snapshot.get("url") or link["url"])
                job = linkedin.job_from_browser_snapshot(
                    snapshot, source_ref=snapshot.get("snapshot_path"))
                job.discovery_cities = [target_city]
                jobs.append(job)
            except linkedin.LinkedInCheckpoint as exc:
                checkpoint = str(exc)
                access_control.note_restricted(workspace, "linkedin", reason=str(exc),
                                               kind="captcha", run_id=run_id)
                break
            except linkedin.LinkedInLoginRequired as exc:
                login_required = str(exc)
                if state is not None:
                    workflow.invalidate_auth(state, "linkedin", login_required)
                break
            except (linkedin.LinkedInAccessError, BrowserError, OSError) as exc:
                problems.append({"job_id": link["job_id"], "url": link["url"],
                                 "reason": str(exc)})
    finally:
        lock.release()
        if browser is None and tab_id:
            try:
                service.close_tab(tab_id)  # 只关本次的标签页，保留登录窗口
            except Exception:
                pass

    jobs = deduplicate(jobs)
    results = [r for j in jobs for r in evaluate(j, config)]
    access_status = ("restricted" if checkpoint else
                     "login_required" if login_required else "success")
    reports = [dict(channel="linkedin", url=url,
                    access_status=access_status,
                    parse_status="success" if jobs else ("partial" if problems else "not_run"),
                    jobs_extracted=len(jobs),
                    reason=("；".join(filter(None, [
                        checkpoint or "",
                        login_required or "",
                        f"{len(problems)} 条详情未解析" if problems else "",
                        "仅浏览器辅助小样本，未翻页、未覆盖站内全部岗位",
                    ]))),
                    browser_pages_read=pages_read, problems=problems)]
    summary = {
        "mode": "browser_assisted",
        "keywords": keywords_text,
        "location": target_city,
        "pages_read": pages_read,
        "detail_limit": limit,
        "jobs_found": len(jobs),
        "checkpoint": checkpoint,
        "login_required": login_required,
        "problems": problems,
        "limits": profile.get("sample_limits"),
        "actions": actions,
        "run_id": run_id,
    }
    discovery = {
        "provider": "jobfinder_local_browser",
        "status": "browser_assisted_sample",
        "operation": "linkedin_browser_search",
        "channel": "linkedin",
        "coverage": (f"领英站内搜索「{keywords_text}」@ {target_city}：读取 {pages_read} 个页面、"
                     f"{len(jobs)} 条岗位原文；不翻页，不代表站内全部岗位"),
        "live_refetched": True,
        "linkedin": profile,
        "linkedin_browser_search": summary,
        "search_log": [],
        "candidates": [{"company": j.company, "title": j.title, "url": j.source_url,
                        "candidate_type": "job_detail", "target_cities": [target_city],
                        "source_channel": "linkedin"} for j in jobs],
        "effective_config": effective,
        "browser_actions": actions,
    }
    if state is not None:
        workflow.record_browser_search(state, "linkedin", summary)
        completion = workflow.finalize(state, persist=False)
        completion["partial_export"] = completion.get("verdict") != "COMPLETE"
        completion["reasons"].append(
            "领英为浏览器辅助小样本：仅读取 1 个搜索页与有限详情，任务簿未因此判定该范围到底")
    else:
        state = workflow.state_from_plan_logs({"plan_id": "linkedin-browser-" + hashlib.sha256(
            (url + target_city).encode()).hexdigest()[:8], "channels": ["linkedin"],
            "effective_config": {"budget": config.budget.model_dump(mode="json")}}, [], config, reports)
        state["scope"]["cities"] = [target_city]
        completion = workflow.finalize(state)
        completion["reasons"].append("领英为浏览器辅助小样本，覆盖范围仅限本次读取的页面")
        completion["partial_export"] = True
    discovery["completion"] = completion
    discovery["run_id"] = completion["run_id"]
    payload = export_bundle(output, config, jobs, results, reports, [], discovery, events=[], mode=mode)
    if run_id is not None:
        workflow.save_state(state, workspace)
    return payload


def reexport(config, existing, output, effective=None):
    old=json.loads(local_path(existing).read_text(encoding="utf-8"))
    mode=old.get("mode","real")
    output=output_path(output,mode)
    jobs=deduplicate([Job.model_validate(j) for j in old["jobs"]])
    # Ignore ALL old results, cached MATCH flags and old geography rule versions.
    results=[r for j in jobs for r in evaluate(j,config)]
    discovery=old.get("discovery",{}) | {"provider":"cache_revalidation","status":"revalidated_not_refetched","origin_provider":old.get("discovery",{}).get("provider"),"coverage":"本次仅对历史原文证据执行当前规则，未再次联网读取；原始覆盖："+old.get("discovery",{}).get("coverage","未记录"),"cache_revalidated_at":now(),"live_refetched":False}
    discovery.update(operation="reexport",config_fingerprint=fingerprint(config),historical_search_log=discovery.get("search_log") or discovery.get("historical_search_log",[]),search_log=[])
    discovery["effective_config"]=effective
    discovery['historical_entry_log'] = discovery.pop('entry_log',[])
    discovery['historical_follow_up_plan'] = discovery.pop('follow_up_plan',None)
    discovery['entry_tasks'] = []
    discovery['historical_traversal_progress']=discovery.pop('traversal_progress',None)
    for key in ('core_execution','company_processing','continuation_requests','read_attempts','input_artifacts'):
        if key in discovery:
            discovery['historical_'+key]=discovery.pop(key)
    reports=[r | {"historical_access_status":r.get("historical_access_status",r.get("access_status")),"historical_parse_status":r.get("historical_parse_status",r.get("parse_status")),"historical_jobs_extracted":r.get("historical_jobs_extracted",r.get("jobs_extracted")),"historical_reason":r.get("historical_reason",r.get("reason")),"jobs_extracted":None,"access_status":"not_run","parse_status":"not_run","attempted":False,"reason":"本次仅重新导出；旧状态/时间保留在历史字段"} for r in old.get("reports",[])]
    return export_bundle(output,config,jobs,results,reports,old.get("companies",[]),discovery,events=old.get("network_events",[]),mode=mode)
