import React, {useEffect, useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {ArrowRight, Check, Clapperboard, Download, Film, Layers, LoaderCircle, LogOut, Pencil, Play, RefreshCw, Settings2, Sparkles, WandSparkles, X} from 'lucide-react'
import './style.css'
import './projects.css'
import {ContinuityPanel, type GenerationConfig} from './ContinuityPanel'
import {PromptPreview} from './PromptPreview'
import {JobError, type ErrorDetails} from './JobError'
import {BackupStatus, type Backup} from './BackupStatus'
import {MediaDownload, MediaVideo} from './Media'

type Spec = {duration:number; scene:string; characters:string[]; action:string; camera:string; dialogue:string; sound:string; start_state?:string; end_state?:string; continuity_notes?:string}
type Shot = {id:string; position:number; spec:Spec; status:string; asset_id:string|null; backup?:Backup|null; media_ready:boolean; generation_config:GenerationConfig; edit_config:{out_frame_exclusive?:number}; reference_stale:boolean; needs_regeneration:boolean}
type Job = {id:string; shot_id:string|null; kind:string; status:string; phase:string; external_id:string|null; error:string|null; error_details?:ErrorDetails|null; attempts:number; next_run:string; max_retries?:number; timeout_seconds?:number; deadline_at?:string}
type VideoSettings = {resolution:string;generate_audio:boolean;watermark:boolean;execution_expires_after:number}
type Form = {name:string; brief:string; style:string; characters:string; duration:number; ratio:string; script_model:string; video_model:string; video_settings:VideoSettings}
type Project = Form & {id:string; shots:Shot[]; jobs:Job[]; script:{title?:string; synopsis?:string; style_bible?:string}; export_asset:string|null; export_backup?:Backup|null; export_ready:boolean}
type ModelOption = {id:string;name:string}
type Config = {script_model:string; video_model:string; provider:string; storage:string; key_configured:boolean;model_catalog:{script:ModelOption[];video:ModelOption[]};ratios:string[]}
type ProjectSummary = {id:string;name:string;archived:boolean;ratio:string;duration:number;video_model:string;active_jobs:number}
const defaultForm = (config:Config|null):Form => ({name:'',brief:'',style:'电影感，温暖自然的光线',characters:'',duration:30,ratio:'16:9',script_model:config?.script_model||'',video_model:config?.video_model||'',video_settings:{resolution:'720p',generate_audio:true,watermark:false,execution_expires_after:172800}})
const ratioLabels:Record<string,string> = {'16:9':'16:9 · 横屏','9:16':'9:16 · 竖屏','1:1':'1:1 · 方形','4:3':'4:3 · 横屏','3:4':'3:4 · 竖屏','21:9':'21:9 · 宽银幕'}
function ModelField({label,value,options,onChange}:{label:string;value:string;options:ModelOption[];onChange:(value:string)=>void}) {
  return <label className="full">{label}<select aria-label={label+'选择'} value={options.some(o=>o.id===value)?value:'custom'} onChange={e=>onChange(e.target.value==='custom'?'':e.target.value)}>
    {options.map(o=><option key={o.id} value={o.id}>{o.name}</option>)}<option value="custom">自定义 Model ID / Endpoint</option>
  </select><input aria-label={label+' Model ID'} value={value} onChange={e=>onChange(e.target.value)} required maxLength={150}/>
    <small>可选择工作室配置的模型，或填写本账户已开通的 Model ID／推理接入点。保存不调用模型。</small></label>
}
const active = ['queued','running','waiting','uncertain']
const labels:Record<string,string> = {draft:'待生成', queued:'排队中', running:'处理中', waiting:'生成中', uncertain:'提交待核对', failed:'失败', succeeded:'已完成', cancelled:'已取消'}
const jobLabel = (job:Job) => job.kind==='script'&&job.status==='waiting' ? '等待重试' : labels[job.status]||job.status
let csrf = ''
async function api<T>(path:string, method='GET', data?:unknown):Promise<T> {
  const response = await fetch('/api'+path, {
    method, credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
    body:method==='GET' ? undefined : JSON.stringify(data ?? {}),
  })
  const body = await response.json().catch(()=>({}))
  if (!response.ok) {
    const detail = body.detail
    throw new Error(typeof detail==='string' ? detail : Array.isArray(detail) ? detail.map((d:{msg:string})=>d.msg).join('；') : `请求失败 (${response.status})`)
  }
  return body
}
function App() {
  const [logged,setLogged] = useState(false)
  const [checking,setChecking] = useState(true)
  const [password,setPassword] = useState('')
  const [config,setConfig] = useState<Config|null>(null)
  const [project,setProject] = useState<Project|null>(null)
  const [projects,setProjects] = useState<ProjectSummary[]>([])
  const selectedProject = useRef(localStorage.getItem('frame-studio-project')||'')
  const loadVersion = useRef(0)
  const [creating,setCreating] = useState(false)
  const [managing,setManaging] = useState(false)
  const [search,setSearch] = useState('')
  const [showArchived,setShowArchived] = useState(false)
  const [form,setForm] = useState<Form>(defaultForm(null))
  const [tab,setTab] = useState('story')
  const [settings,setSettings] = useState(false)
  const [edit,setEdit] = useState<Shot|null>(null)
  const [error,setError] = useState('')
  const [pending,setPending] = useState(false)
  const [notice,setNotice] = useState('')
  const [recovery,setRecovery] = useState<Job|null>(null)
  const [externalId,setExternalId] = useState('')
  const [ack,setAck] = useState(false)
  const [continuity,setContinuity] = useState<string|null>(null)
  const [promptShot,setPromptShot] = useState<string|null>(null)
  const busy = !!project?.jobs.some(j=>active.includes(j.status))
  const generated = project?.shots.filter(s=>s.asset_id && s.status==='succeeded'&&!s.needs_regeneration&&!s.reference_stale).length ?? 0
  const complete = project?.shots.filter(s=>s.media_ready && s.status==='succeeded'&&!s.needs_regeneration&&!s.reference_stale).length ?? 0
  const total = project?.shots.length ?? 0
  async function load() {
    const version=++loadVersion.current
    const list = await api<ProjectSummary[]>('/projects?include_archived=true')
    if(version!==loadVersion.current)return
    setProjects(list)
    const available=list.filter(p=>!p.archived)
    const id=available.find(p=>p.id===selectedProject.current)?.id||available[0]?.id||''
    selectedProject.current=id
    if(!id){localStorage.removeItem('frame-studio-project');setProject(null);return}
    localStorage.setItem('frame-studio-project',id)
    const current=await api<Project>('/projects/'+id)
    if(version===loadVersion.current&&id===selectedProject.current)setProject(current)
  }
  function closeEditors() {setSettings(false);setEdit(null);setContinuity(null);setPromptShot(null);setRecovery(null);setCreating(false)}
  async function selectProject(id:string) {
    selectedProject.current=id;loadVersion.current++
    setProject(null);closeEditors();setManaging(false);setTab('story');setError('');setNotice('');setPending(true)
    try {await load()}catch(e){setError((e as Error).message)}finally{setPending(false)}
  }
  function newProject() {closeEditors();setManaging(false);setForm(defaultForm(config));setCreating(true);setError('');setNotice('')}
  async function createProject() {
    const result=await api<{id:string}>('/projects','POST',form)
    selectedProject.current=result.id;loadVersion.current++;setCreating(false);setProject(null);setTab('story')
  }
  useEffect(()=>{
    api<{csrf:string}>('/session').then(s=>{csrf=s.csrf;setLogged(true)}).catch(()=>{}).finally(()=>setChecking(false))
  },[])
  useEffect(()=>{
    if (!logged) return
    api<Config>('/config').then(c=>{
      setConfig(c);setForm(f=>({...f,script_model:c.script_model,video_model:c.video_model}))
    }).catch(e=>setError(e.message))
    load().catch(e=>setError(e.message))
    const interval = setInterval(()=>load().catch(e=>setError(e.message)),2500)
    return ()=>{clearInterval(interval);loadVersion.current++}
  },[logged])
  async function act(fn:()=>Promise<unknown>, message='') {
    setPending(true);setError('');setNotice('')
    try {await fn();if(logged) await load();setNotice(message)}
    catch(e) {setError((e as Error).message)}
    finally {setPending(false)}
  }
  function change(key:keyof Form, value:string|number) {setForm(f=>({...f,[key]:value}))}
  function changeVideo(key:keyof VideoSettings,value:string|boolean|number) {setForm(f=>({...f,video_settings:{...f.video_settings,[key]:value}}))}
  async function generateScript() {
    if (!project) return
    if (project.shots.length && !confirm('重新生成剧本将替换当前分镜。历史素材会保留，但需重新生成镜头。继续？')) return
    await act(()=>api(`/projects/${project.id}/script`,'POST'),'剧本任务已加入队列')
  }
  async function cancelScript(job:Job) {
    await act(()=>api(`/jobs/${job.id}/cancel`,'POST'),'剧本任务已取消，可调整设置后重新生成；已发出的模型请求可能仍会计费')
  }
  async function downloadScript() {
    if (!project) return
    const data = await api(`/projects/${project.id}/export`)
    const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}))
    const a = document.createElement('a');a.href=url;a.download='storyboard.json';a.click();URL.revokeObjectURL(url)
  }
  if(checking) return <div className="loading"><LoaderCircle className="spin"/> 正在连接工作室</div>
  if(!logged) return <div className="login-page">
    <div className="login-art"><div className="brand"><Clapperboard/> 帧序 <small>FRAME STUDIO</small></div><div className="login-copy"><span className="eyebrow">YOUR STORY, IN MOTION</span><h1>让故事<br/>有自己的画面。</h1><p>从一个想法，到一段值得分享的影像。<br/>你的私人 AI 创作工作室。</p><div className="film-lines"><span/><span/><span/><span/></div></div><span className="art-footer">创意 / 剧本 / 分镜 / 成片</span></div>
    <form className="login-form" onSubmit={e=>{e.preventDefault();act(async()=>{const s=await api<{csrf:string}>('/login','POST',{password});csrf=s.csrf;setLogged(true);setPassword('')})}}>
      <span className="eyebrow">WELCOME BACK</span><h2>进入创作工作室</h2><p>使用本地管理员密码登录</p>
      <label>工作室密码<input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required autoFocus/></label>
      {error && <div className="alert">{error}</div>}<button className="primary" disabled={pending}>{pending?<LoaderCircle className="spin" size={18}/>:<ArrowRight size={18}/>} 进入工作室</button><small>密码位于本机 secrets/owner_password 文件。<br/>模型密钥只保存在服务端。</small>
    </form>
  </div>
  const formFields = <div className="fields">
    <label className="full">作品名称<input value={form.name} onChange={e=>change('name',e.target.value)} placeholder="为你的故事取个名字" required maxLength={100}/></label>
    <label className="full">故事创意<textarea rows={4} value={form.brief} onChange={e=>change('brief',e.target.value)} placeholder="谁是主角？发生了什么？你想传递怎样的情绪？" required minLength={5} maxLength={6000}/></label>
    <label>视觉风格<textarea rows={3} value={form.style} onChange={e=>change('style',e.target.value)}/></label>
    <label>角色设定<textarea rows={3} value={form.characters} onChange={e=>change('characters',e.target.value)} placeholder="外貌、性格与服装，帮助保持角色连续性"/></label>
    <label>目标时长<select value={form.duration} onChange={e=>change('duration',Number(e.target.value))}>{[15,30,60,90,120,180].map(n=><option key={n} value={n}>{n} 秒</option>)}</select></label>
    <label>画面比例<select value={form.ratio} onChange={e=>change('ratio',e.target.value)}>{(config?.ratios||Object.keys(ratioLabels)).map(r=><option key={r} value={r}>{ratioLabels[r]||r}</option>)}</select><small>用于镜头生成与成片导出；已有分镜也可修改。变更后需手动重生成，首帧比例须匹配。</small></label>
    <label>生成与导出分辨率<select value={form.video_settings.resolution} onChange={e=>changeVideo('resolution',e.target.value)}><option>480p</option><option>720p</option><option>1080p</option></select><small>同时用于新镜头与成片导出；fast / mini 模型不支持 1080p。</small></label>
    <label>生成声音<select value={String(form.video_settings.generate_audio)} onChange={e=>changeVideo('generate_audio',e.target.value==='true')}><option value="true">有声：模型生成匹配的声音</option><option value="false">无声：生成与导出均无音轨</option></select></label>
    <label>云端任务超时（小时，1–72）<input type="number" min={1} max={72} step={1} value={form.video_settings.execution_expires_after/3600} onChange={e=>changeVideo('execution_expires_after',Number(e.target.value)*3600)}/><small>从云端受理起计算，默认 48 小时；超时后可核对并恢复任务。</small></label>
    <label>画面水印<select value={String(form.video_settings.watermark)} onChange={e=>changeVideo('watermark',e.target.value==='true')}><option value="false">不显示水印</option><option value="true">显示“AI 生成”水印</option></select><small>规格变更后已有画面标记为旧版本，需手动生成才能应用；不会自动调用模型。</small></label>
    <ModelField label="剧本模型" value={form.script_model} options={config?.model_catalog.script||[]} onChange={v=>change('script_model',v)}/>
    <ModelField label="视频模型" value={form.video_model} options={config?.model_catalog.video||[]} onChange={v=>change('video_model',v)}/>
  </div>
  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><Clapperboard/> 帧序 <small>FRAME STUDIO</small></div><div className="workspace"><span className="workspace-icon">私</span><div>私人工作室<small>本地 · 单用户</small></div></div>
      <div className="project-switcher"><label>当前作品<select aria-label="切换项目" value={project?.id||''} disabled={pending} onChange={e=>selectProject(e.target.value)}><option value="" disabled>选择作品</option>{projects.filter(p=>!p.archived).map(p=><option key={p.id} value={p.id}>{p.name}{p.active_jobs?' · 执行中':''}</option>)}</select></label><div><button className="text-button" disabled={pending||!config} onClick={newProject}>新建作品</button><button className="text-button" disabled={pending} onClick={()=>{setManaging(true);setSearch('');setError('')}}>管理项目</button></div></div>
      <span className="nav-caption">创作空间</span><button className={'nav-item '+(tab==='story'?'selected':'')} onClick={()=>setTab('story')}><Layers size={19}/> 剧本与分镜</button><button className={'nav-item '+(tab==='jobs'?'selected':'')} onClick={()=>setTab('jobs')}><RefreshCw size={19}/> 任务记录{busy&&<i/>}</button><button className={'nav-item '+(tab==='film'?'selected':'')} onClick={()=>setTab('film')}><Film size={19}/> 我的成片</button>
      <div className="sidebar-bottom"><div className="connection"><span className={config?.key_configured?'dot':'dot amber'}/>{config?.key_configured?'方舟密钥已配置':'等待配置方舟密钥'}<small>素材保存于{config?.storage || '工作室'}</small></div><button className="nav-item" disabled={pending} onClick={()=>act(async()=>{await api('/logout','POST');loadVersion.current++;setLogged(false);setProject(null);setProjects([]);closeEditors();setManaging(false)})}><LogOut size={17}/> 退出工作室</button></div>
    </aside>
    <main><header><div className="breadcrumb">工作室 <span>/</span> {project?.name || '新建作品'}</div><span className="private-badge">私人创作</span></header>
      <div className="main-content">
        {error&&<div className="alert" role="alert">{error}<button onClick={()=>setError('')} aria-label="关闭"><X size={16}/></button></div>}
        {notice&&<div className="notice" role="status"><Check size={17}/>{notice}</div>}
        {!config?.key_configured&&<div className="key-note">填写 secrets/ark_api_key 后即可调用真实模型。当前没有生成测试画面或虚构进度。</div>}
        {!project?<><div className="page-title"><span className="eyebrow">START WITH AN IDEA</span><h1>你的下一部作品，从这里开始</h1><p>描述故事、设定角色，再让 AI 为你搭建分镜。</p></div>{pending?<p className="muted">正在载入作品…</p>:<form className="create-card" onSubmit={e=>{e.preventDefault();act(createProject,'项目已创建，可以生成剧本')}}>{formFields}<div className="form-footer"><span>多项目创作 · 每部最长 180 秒</span><button className="primary" disabled={pending||!config}><Sparkles size={18}/> 创建作品</button></div></form>}</>:
        <>
          <div className="project-heading"><div><span className="eyebrow">CREATIVE PROJECT</span><h1>{project.name}</h1><p>{project.duration} 秒 <b>·</b> {project.ratio} <b>·</b> {project.style || '自由风格'}</p></div><button className="secondary" disabled={busy||pending} onClick={()=>{setForm({...project});setSettings(true)}}><Settings2 size={17}/> 项目设置</button></div>
          <div className="steps">{['故事设定','剧本分镜','镜头生成','预览成片'].map((s,i)=>{
            const stage=project.export_ready?4:complete===total&&total>0?3:total>0?2:1
            return <div className={i<stage?'step reached':'step'} key={s}><span>{i+1<stage?<Check size={14}/>:i+1}</span>{s}{i<3&&<div className="step-line"/>}</div>
          })}</div>
          {tab==='story'&&<>
            <section className="story-card"><div className="section-heading"><h2><WandSparkles size={19}/> 故事蓝图</h2><button className="text-button" disabled={pending||busy} onClick={generateScript}><RefreshCw size={15}/>{total?'重新生成剧本':'生成剧本'}</button></div><h3>{project.script.title || '一个好故事，值得被看见'}</h3><p>{project.script.synopsis || project.brief}</p>{project.script.style_bible&&<div className="style-bible"><span>视觉设定</span>{project.script.style_bible}</div>}</section>
            <div className="section-heading storyboard-heading"><div><h2>分镜工作台 <span className="count">{total}</span></h2><p>先审阅分镜，再生成画面。支持逐镜头编辑与重生成。</p></div><div className="actions">{total>0&&<button className="secondary" onClick={()=>act(downloadScript)}><Download size={16}/> 导出剧本</button>}<button className="primary" disabled={!total||busy||pending||generated===total} onClick={()=>{if(confirm(`将生成 ${total-generated} 个未完成镜头，方舟调用可能计费。继续？`))act(()=>api(`/projects/${project.id}/videos`,'POST'),'镜头任务已加入队列')}}><Play size={16}/> 生成镜头</button></div></div>
            {!total?<div className="empty"><Clapperboard size={38}/><h3>分镜即将登场</h3><p>{busy?'剧本正在生成，完成后会自动显示。':'点击“生成剧本”，将创意转化为可编辑的镜头。'}</p></div>:<div className="shot-grid">{project.shots.map(shot=><article className="shot-card" key={shot.id}>
              <div className="shot-preview" style={{aspectRatio:project.ratio.replace(':',' / ')}}>{shot.asset_id?<MediaVideo assetId={shot.asset_id} controls preload="metadata"/>:<div className="shot-placeholder"><Clapperboard size={28}/><span>{shot.status==='draft'?'准备好，让故事动起来':labels[shot.status]||shot.status}</span>{['running','waiting','queued'].includes(shot.status)&&<LoaderCircle className="spin" size={18}/>}</div>}<span className="shot-number">SHOT {String(shot.position).padStart(2,'0')}</span><span className="duration">{shot.spec.duration}s</span></div>
              <div className="shot-body"><div className="shot-title"><h3>{shot.spec.scene}</h3><span className={'status '+shot.status}>{shot.status==='succeeded'&&!shot.media_ready?'已生成 · 待上传':labels[shot.status]||shot.status}</span></div><p>{shot.spec.action}</p><div className="shot-detail"><span>运镜</span>{shot.spec.camera}</div>
                {shot.asset_id&&<BackupStatus assetId={shot.asset_id} data={shot.backup} request={api}/>}
                {shot.spec.start_state&&<div className="shot-detail"><span>开始</span>{shot.spec.start_state}</div>}
                {shot.spec.end_state&&<div className="shot-detail"><span>结束</span>{shot.spec.end_state}</div>}
                <div className="shot-detail"><span>生成</span>{{text:'文字生成',first_frame:'首帧生成',first_last_frame:'首尾帧',reference:'多模态参考'}[shot.generation_config.mode]}{shot.edit_config.out_frame_exclusive&&` · 出点 ${(shot.edit_config.out_frame_exclusive/24).toFixed(3)}s`}</div>
                {shot.reference_stale?<p className="job-error">参考来源已变化，请重新选帧。</p>:shot.asset_id&&shot.needs_regeneration?<p className="job-error">生成配置已更新，现有画面为旧版本。</p>:null}
                {shot.spec.dialogue&&<blockquote>“{shot.spec.dialogue}”</blockquote>}<div className="shot-footer"><button className="text-button" disabled={busy||pending} onClick={()=>setEdit({...shot,spec:{...shot.spec}})}><Pencil size={14}/> 编辑</button><button className="text-button" disabled={busy||pending} onClick={()=>setContinuity(shot.id)}>参考素材</button><button className="text-button" onClick={()=>setPromptShot(shot.id)}>提示词</button><button className="text-button" disabled={busy||pending||shot.reference_stale} onClick={()=>{if(confirm('生成此镜头会调用方舟模型，可能计费。继续？'))act(()=>api(`/projects/${project.id}/shots/${shot.id}/generate`,'POST'),'镜头任务已加入队列')}}><RefreshCw size={14}/>{shot.asset_id?'重生成':'生成'}</button></div></div>
            </article>)}</div>}
            {total>0&&<div className="composition-bar"><div><Film size={24}/><div><strong>{complete} / {total} 个镜头已就绪</strong><p>按确认的剪辑出点硬切，保留原生音频</p></div></div><button className="primary" disabled={busy||pending||complete!==total} onClick={()=>act(()=>api(`/projects/${project.id}/compose`,'POST'),'成片拼接已加入队列')}><Layers size={17}/> 拼接成片</button></div>}
          </>}
          {tab==='jobs'&&<><div className="section-heading"><h2>任务记录</h2><span className="muted">每 2.5 秒自动更新</span></div><p className="muted">剧本失败后最多自动重试 2 次，总时限 10 分钟；可取消任务后调整设置。提交待核对的视频任务，请先在方舟控制台查证。</p><div className="job-list">{project.jobs.length===0?<div className="empty"><RefreshCw/><h3>还没有任务</h3><p>从剧本与分镜开始创作。</p></div>:project.jobs.map(job=><div className="job-row" key={job.id}><div className={'job-icon '+job.status}>{job.kind==='script'?<WandSparkles size={20}/>:job.kind==='compose'?<Film size={20}/>:<Clapperboard size={20}/>}</div><div className="job-info"><strong>{job.kind==='script'?'剧本生成':job.kind==='compose'?'成片拼接':`镜头 ${project.shots.find(s=>s.id===job.shot_id)?.position || '历史'} 生成`}</strong><small>{job.id.slice(0,8)} · {job.phase} {job.external_id&&`· 方舟 ID: ${job.external_id}`}</small>{job.kind==='script'&&<small>已失败 {job.attempts} 次 · 最多自动重试 {job.max_retries??2} 次{job.status==='waiting'&&job.next_run&&` · 下次重试 ${new Date(job.next_run).toLocaleTimeString()}`}{active.includes(job.status)&&job.deadline_at&&` · 截止 ${new Date(job.deadline_at).toLocaleTimeString()}`}</small>}<JobError job={job}/></div><span className={'status '+job.status}>{jobLabel(job)}</span>{job.kind==='script'&&['queued','running','waiting'].includes(job.status)&&<button className="secondary" disabled={pending} onClick={()=>cancelScript(job)}>取消剧本任务</button>}{['failed','uncertain'].includes(job.status)&&<button className="secondary" disabled={pending} onClick={()=>{setRecovery(job);setExternalId(job.external_id||'');setAck(false)}}>恢复任务</button>}</div>)}</div></>}
          {tab==='film'&&<><div className="section-heading"><h2>我的成片</h2>{project.export_asset&&project.export_ready&&<MediaDownload assetId={project.export_asset}><Download size={17}/> 下载 MP4</MediaDownload>}</div>{project.export_asset?<div className="final-film"><MediaVideo controls assetId={project.export_asset} preload="metadata"/><div><h3>{project.name}</h3><BackupStatus assetId={project.export_asset} data={project.export_backup} request={api}/><p>成片保留模型原生音频。剧本台词可能与实际语音不同。</p></div></div>:<div className="empty"><Film size={40}/><h3>你的作品，值得期待</h3><p>完成所有镜头后，在分镜工作台点击“拼接成片”。</p><button className="secondary" onClick={()=>setTab('story')}>返回分镜工作台</button></div>}</>}
        </>}
        <footer>帧序 FRAME STUDIO <span>故事由你定义，画面由创意延伸。</span></footer>
      </div>
    </main>
    {settings&&<div className="modal-backdrop"><form className="modal" onSubmit={e=>{e.preventDefault();act(async()=>{await api(`/projects/${project!.id}`,'PUT',form);setSettings(false)},'项目设置已保存')}}><div className="section-heading"><h2>项目设置</h2><button type="button" className="icon-button" disabled={pending} onClick={()=>setSettings(false)} aria-label="关闭"><X/></button></div><p className="muted">更换剧本模型用于下一次剧本生成；更换视频模型或画幅后已有画面标记为旧版本，需手动重新生成。历史素材保留。</p>{error&&<div className="alert" role="alert">{error}</div>}{formFields}<button className="primary" disabled={pending}>保存设置</button></form></div>}
    {creating&&<div className="modal-backdrop"><form className="modal" onSubmit={e=>{e.preventDefault();act(createProject,'项目已创建，可以生成剧本')}}><div className="section-heading"><h2>新建作品</h2><button className="icon-button" type="button" disabled={pending} aria-label="关闭" onClick={()=>setCreating(false)}><X/></button></div>{error&&<div className="alert" role="alert">{error}</div>}{formFields}<button className="primary" disabled={pending||!config}>创建作品</button></form></div>}
    {managing&&<div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label="项目管理"><div className="section-heading"><h2>项目管理</h2><button className="icon-button" disabled={pending} aria-label="关闭" onClick={()=>setManaging(false)}><X/></button></div><p className="muted">各项目独立保存剧本、模型、任务和成片。归档保留全部数据，执行中的项目须先完成或核对任务。</p>{error&&<div className="alert" role="alert">{error}</div>}<label>搜索作品<input value={search} onChange={e=>setSearch(e.target.value)}/></label><label className="checkbox"><input type="checkbox" checked={showArchived} onChange={e=>setShowArchived(e.target.checked)}/>显示已归档项目</label><button className="secondary" disabled={pending||!config} onClick={newProject}>新建作品</button>
      <div className="project-list">{projects.filter(p=>(showArchived||!p.archived)&&p.name.toLowerCase().includes(search.toLowerCase())).map(p=><article className="project-item" key={p.id}><strong>{p.name}{p.id===project?.id?' · 当前':''}</strong><small>{p.ratio} · {p.duration} 秒 · {p.archived?'已归档':p.active_jobs?'任务执行中':'可创作'}</small><small>{p.video_model}</small><div>{!p.archived&&<button className="secondary" disabled={pending} onClick={()=>selectProject(p.id)}>打开</button>}<button className="text-button" disabled={pending||!!p.active_jobs} onClick={()=>act(async()=>{await api(`/projects/${p.id}/archive`,'PUT',{archived:!p.archived});if(p.id===selectedProject.current&&!p.archived){selectedProject.current='';setProject(null);setForm(defaultForm(config));closeEditors()}},p.archived?'项目已恢复':'项目已归档，素材保留')}>{p.archived?'恢复':'归档'}</button></div></article>)}</div></section></div>}
    {edit&&<div className="modal-backdrop"><form className="modal" onSubmit={e=>{e.preventDefault();act(async()=>{await api(`/projects/${project!.id}/shots/${edit.id}`,'PUT',edit.spec);setEdit(null)},'分镜已保存')}}><div className="section-heading"><h2>编辑镜头 {edit.position}</h2><button className="icon-button" type="button" onClick={()=>setEdit(null)} aria-label="关闭"><X/></button></div><p className="muted">修改后需要重新生成此镜头并拼接成片。</p>
      {(['scene','action','camera','dialogue','sound','start_state','end_state','continuity_notes'] as const).map((key,i)=><label key={key}>{['场景','动作','运镜','台词','声音','开始状态（朝向、位置、大小与动作阶段）','结束状态（下一镜头需承接的姿态）','连续性约束（场景、视线与运动方向）'][i]}<textarea rows={2} value={edit.spec[key]||''} required={i<3} onChange={e=>setEdit({...edit,spec:{...edit.spec,[key]:e.target.value}})}/></label>)}<label>镜头时长（4–15 秒）<input type="number" min={4} max={15} required value={edit.spec.duration} onChange={e=>setEdit({...edit,spec:{...edit.spec,duration:Number(e.target.value)}})}/></label><button className="primary" disabled={pending}>保存分镜</button></form></div>}
    {continuity&&project&&project.shots.find(s=>s.id===continuity)&&<ContinuityPanel key={continuity} projectId={project.id} shot={project.shots.find(s=>s.id===continuity)!} previous={project.shots.find(s=>s.position===project.shots.find(t=>t.id===continuity)!.position-1)} request={api} onSaved={load} onClose={()=>setContinuity(null)}/>}
    {promptShot&&project&&<PromptPreview projectId={project.id} shotId={promptShot} request={api} onClose={()=>setPromptShot(null)}/>}
    {recovery&&<div className="modal-backdrop"><form className="modal small-modal" onSubmit={e=>{e.preventDefault();act(async()=>{await api(`/jobs/${recovery.id}/recover`,'POST',{external_id:externalId||null,acknowledge_duplicate_cost:ack});setRecovery(null)},'任务已恢复')}}><div className="section-heading"><h2>恢复任务</h2><button type="button" className="icon-button" onClick={()=>setRecovery(null)} aria-label="关闭"><X/></button></div><p>{recovery.error}</p>{error&&<div className="alert">{error}</div>}{recovery.kind==='video'&&<>{recovery.phase!=='rejected'&&<label>方舟已受理任务 ID<input value={externalId} onChange={e=>setExternalId(e.target.value)} placeholder="核对控制台后填写"/></label>}{((recovery.status==='uncertain'&&!externalId)||recovery.phase==='rejected')&&<label className="checkbox"><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/>{recovery.phase==='rejected'?'模型已终止任务，我确认重新生成可能再次计费':'我已核对，确认重新提交可能产生重复费用'}</label>}</>}<button className="primary" disabled={pending||(((recovery.status==='uncertain'&&!externalId)||recovery.phase==='rejected')&&!ack)}>恢复任务</button></form></div>}
  </div>
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>)
