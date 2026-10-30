/** JobFinder's injected, visible-DOM browser reader. No browser launch, HTTP or cookies.
 * Run inside the documented cua_repl runtime with its selected Browser handle.
 * Each generator yield is a durable checkpoint and a chance to report progress.
 */
import {createHash} from 'node:crypto';
import {mkdir, writeFile, rename} from 'node:fs/promises';
import {join} from 'node:path';

const hash = text => createHash('sha256').update(text, 'utf8').digest('hex');
export function pageState(page, spec) {
  if (/验证码|访问被拒绝|Access Denied|verify you are human/i.test(page.text)) return 'restricted';
  if (page.busy || /(?:^|\n)\s*(?:Loading(?:\.{3})?|加载中[.…]*)\s*(?:\n|$)/i.test(page.text)) return 'loading';
  if (page.cards.length) return 'ready';
  if (spec.empty_pattern && new RegExp(spec.empty_pattern).test(page.text)) return 'empty';
  return 'shell';
}

// All selectors must come from an observed page and be recorded in the plan.
async function readDOM(tab, spec, frame = null) {
  const scope = frame ? tab.playwright.frameLocator(frame) : tab.playwright;
  return scope.locator('body').evaluate((body, s) => {
    const visible = e => !!e && !!e.getClientRects().length && e.ownerDocument.defaultView.getComputedStyle(e).visibility !== 'hidden';
    const select = (root, css) => css ? [...root.querySelectorAll(css)].filter(visible) : [];
    const txt = (root, css) => select(root, css)[0]?.innerText?.trim() || '';
    const fields = Object.fromEntries(Object.entries(s.fields || {}).map(([k, css]) => [k, txt(body, css)]));
    const cards = select(body, s.card_selector).map((e, index) => ({
      index, text:e.innerText.trim(), title:txt(e,s.title_selector) || e.innerText.trim().split('\n')[0],
      location:txt(e,s.location_selector), company:txt(e,s.company_selector),
      href:e.getAttribute('href') ? e.href : null,
    }));
    const controls = select(body,s.page_selector).map(e=>({text:e.innerText.trim(), active:/active|current|selected/.test(e.className), disabled:/disabled/.test(e.className)||e.getAttribute('aria-disabled')==='true'}));
    const next = select(body,s.next_selector)[0];
    return {url:body.ownerDocument.URL, title:body.ownerDocument.title, text:body.innerText,
      fields,cards,controls,next:!!next && !/disabled/.test(next.className) && next.getAttribute('aria-disabled')!=='true',
      busy:select(body,'[aria-busy="true"], [role="progressbar"]').length>0,
      frames:[...body.querySelectorAll('iframe')].filter(visible).map(e=>({id:e.id,src:e.src})),
      links:[...body.querySelectorAll('a')].filter(visible).map(e=>({text:e.innerText.trim(),href:e.href})),
      inputs:[...body.querySelectorAll('input')].filter(visible).map(e=>({placeholder:e.placeholder,type:e.type}))};
  }, spec);
}

export function selectedCard(card, spec) {
  return !spec.select_pattern || new RegExp(spec.select_pattern,'i').test(card.text);
}

export function createCollector(browser, plan, outputDir, {mode='real'}={}) {
  if(!['real','test'].includes(mode)) throw new Error('Invalid run mode');
  const run = {schema_version:'browser-run-1',runtime:'codex_browser_visible_dom',mode,
    started_at:new Date().toISOString(),plan,companies:[],snapshots:[],events:[],details:[]};
  async function save() {
    await mkdir(outputDir,{recursive:true});
    const dest=join(outputDir,'browser-run.json');
    await writeFile(dest+'.tmp',JSON.stringify(run,null,2),'utf8');
    await rename(dest+'.tmp',dest);
  }
  function event(company,action,extra={}) {run.events.push({at:new Date().toISOString(),company,action,...extra});}
  async function capture(tab,spec,company,kind,frame=null) {
    const p=await readDOM(tab,spec,frame);
    const snap={id:`s${run.snapshots.length+1}`,company,kind,tab_id:tab.id,frame_selector:frame,
      retrieved_at:new Date().toISOString(),...p,content_hash:hash(p.text)};
    run.snapshots.push(snap); event(company,'read_visible_dom',{snapshot_id:snap.id,url:snap.url});
    await save(); return snap;
  }
  async function* collect(spec) {
    if(run.companies.some(c=>c.company===spec.company)) throw new Error('Company already attempted; use retry(spec) explicitly');
    const company={company:spec.company,entry_url:spec.entry_url,scope:spec.scope,status:'running',
      pages:[],listed:[],selected:[],detail_ids:[],problems:[],coverage_complete:false};
    run.companies.push(company); await save();
    let tab, frame=null, listPage;
    try {
      tab=await browser.tabs.new(); await tab.goto(spec.entry_url);
      event(spec.company,'navigate',{url:spec.entry_url,tab_id:tab.id}); await save(); yield {company:spec.company,step:'opened'};
      let page=await capture(tab,spec,spec.company,'entry'); yield {company:spec.company,step:'entry_read'};
      for (const action of spec.pre_actions || []) {
        if (!action.selector || !['click','fill'].includes(action.action)) throw new Error('Invalid observed preparation action');
        const locator=tab.playwright.locator(action.selector);
        if(action.action==='click') await locator.click();
        else await locator.fill(action.value);
        event(spec.company,'prepare_'+action.action,{selector:action.selector}); await save();
        yield {company:spec.company,step:'preparation_action'};
        page=await capture(tab,spec,spec.company,'prepared_list');
        yield {company:spec.company,step:'prepared_list_read'};
      }
      if (!page.text.trim() && page.frames.length===1 && page.frames[0].id) {
        frame='#'+page.frames[0].id;
        event(spec.company,'enter_iframe',{selector:frame,src:page.frames[0].src});
        page=await capture(tab,spec,spec.company,'iframe',frame); yield {company:spec.company,step:'iframe_read'};
      }
      if (spec.navigation_text) {
        const link=page.links.find(l=>l.text===spec.navigation_text && /^https?:/.test(l.href));
        if (!link) throw new Error('Observed navigation link missing: '+spec.navigation_text);
        // Promote the observed iframe link to an ordinary tab document; retain its parent evidence.
        event(spec.company,'follow_observed_link',{parent_snapshot:page.id,url:link.href});
        await tab.goto(link.href); frame=null; await save(); yield {company:spec.company,step:'navigation_followed'};
        page=await capture(tab,spec,spec.company,'list'); yield {company:spec.company,step:'list_read'};
      }
      const visited=new Set();
      for (let n=0;n<(spec.max_pages||20);n++) {
        let state=pageState(page,spec);
        if (state==='shell'||state==='loading') {
          event(spec.company,'wait_for_business_content',{previous_snapshot:page.id,state});
          const scope=frame?tab.playwright.frameLocator(frame):tab.playwright;
          let ready=scope.locator(spec.card_selector).first();
          if(spec.empty_pattern) ready=ready.or(scope.getByText(new RegExp(spec.empty_pattern)));
          await ready.waitFor({state:'visible',timeoutMs:15000});
          await save(); yield {company:spec.company,step:'business_content_waited'};
          page=await capture(tab,spec,spec.company,'list',frame); state=pageState(page,spec);
          yield {company:spec.company,step:'loaded_list_read',state};
        }
        if (!['ready','empty'].includes(state)) throw new Error('List not ready: '+state);
        const key=hash(JSON.stringify(page.cards));
        if(visited.has(key)) throw new Error('Pagination did not change list; coverage incomplete');
        visited.add(key); company.pages.push(page.id); listPage=page;
        const count=spec.total_pattern ? page.text.match(new RegExp(spec.total_pattern)) : null;
        if(count) company.reported_total=Number(count[1]);
        for(const card of page.cards) {
          const entry={...card,snapshot_id:page.id,selected:selectedCard(card,spec)};
          company.listed.push(entry);
          if (!entry.selected) continue;
          company.selected.push(entry);
          const before=await browser.tabs.list();
          const scope=frame?tab.playwright.frameLocator(frame):tab.playwright;
          await scope.locator(spec.card_selector).nth(card.index).click();
          event(spec.company,'open_detail',{list_snapshot:page.id,card_title:card.title});
          const after=await browser.tabs.list();
          const added=after.filter(t=>!before.some(b=>b.id===t.id));
          let detailTab=tab;
          if(added.length===1) {
            detailTab=await browser.tabs.get(added[0].id);
            event(spec.company,'new_tab',{parent_tab:tab.id,tab_id:detailTab.id,url:added[0].url});
          } else if(added.length>1) throw new Error('Multiple new tabs need review; no arbitrary selection');
          await save(); yield {company:spec.company,step:'detail_opened',title:card.title,new_tab:detailTab!==tab};
          let d=await capture(detailTab,{fields:spec.detail_fields||{}},spec.company,'detail',detailTab===tab?frame:null);
          yield {company:spec.company,step:'detail_read',title:card.title};
          if(!d.text.includes(card.title) || !new RegExp(spec.detail_ready_pattern||'岗位职责|工作职责|职位描述').test(d.text)) {
            await detailTab.playwright.getByText(new RegExp(spec.detail_ready_pattern||'岗位职责|工作职责|职位描述')).first().waitFor({state:'visible',timeoutMs:15000});
            yield {company:spec.company,step:'detail_waited'};
            d=await capture(detailTab,{fields:spec.detail_fields||{}},spec.company,'detail');
            yield {company:spec.company,step:'loaded_detail_read'};
          }
          const id=`d${run.details.length+(run.superseded_details||[]).length+1}`;
          run.details.push({id,company:spec.company,card:entry,snapshot_id:d.id,recruitment_type:spec.recruitment_type});
          company.detail_ids.push(id); await save();
          if(detailTab!==tab) await detailTab.close();
          else {await tab.back(); event(spec.company,'return_to_list'); yield {company:spec.company,step:'back_to_list'};
            let restored=await capture(tab,spec,spec.company,'list_restore',frame);
            if(['shell','loading'].includes(pageState(restored,spec))) {
              await scope.locator(spec.card_selector).first().waitFor({state:'visible',timeoutMs:15000});
              yield {company:spec.company,step:'restored_list_waited'};
              restored=await capture(tab,spec,spec.company,'list_restore',frame);
            }
            if(hash(JSON.stringify(restored.cards))!==key) throw new Error('List not restored after same-tab detail');
            yield {company:spec.company,step:'list_restored'};
          }
        }
        const nextNumber=page.controls.find(c=>c.active);
        const numeric=nextNumber && page.controls.find(c=>c.text===String(Number(nextNumber.text)+1)&&!c.disabled);
        const scope=frame?tab.playwright.frameLocator(frame):tab.playwright;
        if(page.next && spec.next_selector) await scope.locator(spec.next_selector).click();
        else if(numeric && spec.page_selector) await scope.locator(spec.page_selector).filter({hasText:new RegExp('^'+numeric.text+'$')}).click();
        else {
          const totalOK=Number.isFinite(company.reported_total)&&company.listed.length===company.reported_total;
          const terminal=spec.terminal_pattern&&new RegExp(spec.terminal_pattern).test(page.text);
          company.coverage_complete=!!(totalOK||terminal||state==='empty');
          company.status=company.coverage_complete?'complete':'partial';
          if(!company.coverage_complete) company.problems.push('No validated end-of-list/count agreement');
          break;
        }
        event(spec.company,'next_page',{from_snapshot:listPage.id}); await save();
        yield {company:spec.company,step:'next_page'};
        page=await capture(tab,spec,spec.company,'list',frame);
        yield {company:spec.company,step:'list_read',cards:page.cards.length};
      }
      if(company.status==='running') {company.status='partial';company.problems.push('Explicit max_pages reached');}
    } catch(error) {
      company.status='blocked'; company.coverage_complete=false; company.problems.push(String(error.message||error));
      event(spec.company,'failure',{reason:company.problems.at(-1)});
    } finally {
      if(company.status==='running') {
        company.status='interrupted'; company.coverage_complete=false;
        company.problems.push('Generator closed before collection completed');
      }
      company.finished_at=new Date().toISOString(); await save();
    }
    company.finished_at=new Date().toISOString(); await save();
    yield {company:spec.company,step:'finished',status:company.status,listed:company.listed.length,details:company.detail_ids.length};
  }
  async function* retry(spec) {
    const index=run.companies.findIndex(c=>c.company===spec.company);
    if(index<0) throw new Error('No previous company attempt');
    const previous=run.companies[index];
    if(previous.status==='running') throw new Error('Finish or close the active generator before retrying');
    run.previous_attempts??=[];
    run.previous_attempts.push({...previous,superseded_at:new Date().toISOString()});
    run.companies.splice(index,1);
    run.superseded_details??=[];
    run.superseded_details.push(...run.details.filter(d=>d.company===spec.company));
    run.details=run.details.filter(d=>d.company!==spec.company);
    event(spec.company,'explicit_retry',{previous_status:previous.status});
    await save();
    yield* collect(spec);
  }
  return {run,save,collect,retry};
}
