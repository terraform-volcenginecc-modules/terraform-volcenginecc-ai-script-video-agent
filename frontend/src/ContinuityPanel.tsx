import {useEffect, useRef, useState} from 'react'
import {X, Play, LoaderCircle} from 'lucide-react'
import './continuity.css'
import {BackupStatus} from './BackupStatus'

type Frame = {asset_id:string; origin?:string; width:number; height:number; source_asset_id?:string; frame_index:number; out_frame_exclusive:number; frame_count:number; time:number}
type Remote = {kind:'video'|'audio';url:string;duration:number}
export type GenerationConfig = {mode:string;reference_asset_id?:string;last_frame_asset_id?:string;reference_image_ids?:string[];remote_references?:Remote[]}
type Shot = {id:string; position:number; asset_id:string|null; generation_config:GenerationConfig; reference_stale:boolean}
type Request = <T>(path:string, method?:string, data?:unknown)=>Promise<T>

export function ContinuityPanel({projectId, shot, previous, request, onSaved, onClose}:{
  projectId:string; shot:Shot; previous?:Shot; request:Request; onSaved:()=>Promise<void>; onClose:()=>void
}) {
  const [mode,setMode] = useState(shot.generation_config.mode)
  const [frame,setFrame] = useState<Frame|null>(null)
  const [lastId,setLastId] = useState(shot.generation_config.last_frame_asset_id||'')
  const [images,setImages] = useState(shot.generation_config.reference_image_ids||[])
  const [remotes,setRemotes] = useState<Remote[]>(shot.generation_config.remote_references||[])
  const [frameNumber,setFrameNumber] = useState('')
  const [working,setWorking] = useState(false)
  const [error,setError] = useState('')
  const [saved,setSaved] = useState(false)
  const [previewing,setPreviewing] = useState(false)
  const firstVideo = useRef<HTMLVideoElement>(null)
  const secondVideo = useRef<HTMLVideoElement>(null)
  const timer = useRef<ReturnType<typeof setTimeout>|null>(null)
  function stopPreview() {
    if (timer.current) clearTimeout(timer.current)
    firstVideo.current?.pause();secondVideo.current?.pause()
    setPreviewing(false)
  }
  useEffect(()=>{
    let live = true
    const id = shot.generation_config.reference_asset_id
    if (id) request<Frame>(`/assets/${id}/info`).then(f=>{
      if(live){setFrame(f);setFrameNumber(f.origin==='uploaded'?'':String(f.frame_index))}
    }).catch(e=>{if(live)setError(e.message)})
    return ()=>{live=false;if(timer.current)clearTimeout(timer.current);firstVideo.current?.pause();secondVideo.current?.pause()}
  },[shot.id])
  async function prepare() {
    if(!previous?.asset_id)return
    stopPreview();setWorking(true);setError('');setSaved(false)
    try {
      const f = await request<Frame>(`/projects/${projectId}/shots/${shot.id}/reference-frame`,'POST',{
        source_shot_id:previous.id,source_asset_id:previous.asset_id,
        frame_index:frameNumber.trim()===''?null:Number(frameNumber),
      })
      setFrame(f);setFrameNumber(String(f.frame_index));setMode(current=>current==='first_last_frame'?current:'first_frame')
    }catch(e){setError((e as Error).message)}
    finally{setWorking(false)}
  }
  async function save() {
    setWorking(true);setError('')
    try {
      await request(`/projects/${projectId}/shots/${shot.id}/generation-settings`,'PUT',{
        mode,reference_asset_id:['first_frame','first_last_frame'].includes(mode)?frame?.asset_id:null,
        last_frame_asset_id:mode==='first_last_frame'?lastId:null,
        reference_image_ids:mode==='reference'?images:[],
        remote_references:mode==='reference'?remotes:[],
      })
      await onSaved();setSaved(true)
    }catch(e){setError((e as Error).message)}
    finally{setWorking(false)}
  }
  async function upload(file:File|undefined, role:'first'|'last'|'reference') {
    if(!file)return
    setWorking(true);setError('');setSaved(false)
    try {
      if(file.size>29*1024*1024)throw new Error('图片须小于 29 MB')
      const data = await new Promise<string>((resolve,reject)=>{
        const reader=new FileReader()
        reader.onload=()=>resolve(String(reader.result).split(',')[1])
        reader.onerror=()=>reject(new Error('读取图片失败'))
        reader.readAsDataURL(file)
      })
      const f=await request<Frame>(`/projects/${projectId}/reference-images`,'POST',{data})
      if(role==='first'){setFrame(f);setFrameNumber('')}
      else if(role==='last')setLastId(f.asset_id)
      else setImages(current=>[...current,f.asset_id])
    }catch(e){setError((e as Error).message)}
    finally{setWorking(false)}
  }
  async function preview() {
    stopPreview();setError('')
    const a=firstVideo.current,b=secondVideo.current
    if(!frame||!a||!b)return
    const duration=Math.min(.65,frame.out_frame_exclusive/24)
    a.currentTime=Math.max(0,frame.out_frame_exclusive/24-duration)
    b.currentTime=0
    try {
      setPreviewing(true)
      await a.play()
      timer.current=setTimeout(async()=>{
        a.pause()
        try {await b.play();timer.current=setTimeout(()=>{b.pause();setPreviewing(false)},1300)}
        catch{setPreviewing(false);setError('请先加载两个镜头，再播放切换预览')}
      },duration*1000)
    }catch{setPreviewing(false);setError('视频尚未就绪，请稍后重试')}
  }
  const currentFrame=!!frame&&(frame.origin==='uploaded'||frame.source_asset_id===previous?.asset_id)
  const firstMode=['first_frame','first_last_frame'].includes(mode)
  return <div className="modal-backdrop"><section className="modal continuity-modal" role="dialog" aria-modal="true" aria-label="镜头参考素材">
    <div className="section-heading"><h2>镜头 {shot.position} · 参考素材</h2><button className="icon-button" onClick={()=>{stopPreview();onClose()}} aria-label="关闭"><X/></button></div>
    <p className="muted">选择首帧控制起始画面，或用多模态素材参考角色、动作和声音。保存后需手动生成才能应用。</p>
    {error&&<div className="alert" role="alert">{error}</div>}
    {saved&&<div className="notice" role="status">配置已保存。需重新生成当前镜头才能看到新的衔接效果。</div>}
    {shot.reference_stale&&<div className="key-note">上一镜头或出点已变化，请重新选帧确认。</div>}
    <label>生成方式<select value={mode} onChange={e=>{setMode(e.target.value);setSaved(false)}} disabled={working}>
      <option value="text">文字生成</option><option value="first_frame">首帧（上传或承接上一镜头）</option>
      <option value="first_last_frame">首尾帧</option><option value="reference">多模态参考</option>
    </select></label>
    {firstMode&&<label>上传首帧（PNG / JPEG，29 MB 内，边长 300–6000）<input type="file" accept="image/png,image/jpeg" disabled={working} onChange={e=>{upload(e.target.files?.[0],'first');e.target.value=''}}/></label>}
    {mode==='first_last_frame'&&<label>上传尾帧（尺寸须与首帧一致）<input type="file" accept="image/png,image/jpeg" disabled={working} onChange={e=>{upload(e.target.files?.[0],'last');e.target.value=''}}/>{lastId&&<><img src={`/api/assets/${lastId}`} alt="选定的尾帧" style={{maxHeight:180,maxWidth:'100%'}}/><BackupStatus assetId={lastId} request={request}/></>}</label>}
    {mode==='reference'&&<>
      <label>上传角色／场景参考图（最多 9 张）<input type="file" accept="image/png,image/jpeg" disabled={working||images.length>=9} onChange={e=>{upload(e.target.files?.[0],'reference');e.target.value=''}}/></label>
      {images.map((id,i)=><div className="reference-frame" key={id}><img src={`/api/assets/${id}`} alt={`参考图片 ${i+1}`}/><div>图片 {i+1}<BackupStatus assetId={id} request={request}/><button className="text-button" disabled={working} onClick={()=>{setImages(images.filter(v=>v!==id));setSaved(false)}}>移除</button></div></div>)}
      <p className="muted">视频／音频需提供可供方舟读取的长期公网 HTTPS 地址，不支持本地文件或临时签名链接。各最多 3 份，总时长各不超过 15 秒；时长请按原素材填写，云端会校验实际素材。</p>
      {remotes.map((r,i)=><div key={i}>
        <label>参考 {i+1} 类型<select disabled={working} value={r.kind} onChange={e=>{setRemotes(remotes.map((v,n)=>n===i?{...v,kind:e.target.value as Remote['kind']}:v));setSaved(false)}}><option value="video">视频</option><option value="audio">音频</option></select></label>
        <label>长期 HTTPS 地址<input disabled={working} type="url" value={r.url} onChange={e=>{setRemotes(remotes.map((v,n)=>n===i?{...v,url:e.target.value}:v));setSaved(false)}}/></label>
        <label>素材时长（2–15 秒）<input disabled={working} type="number" min={2} max={15} step={.1} value={r.duration} onChange={e=>{setRemotes(remotes.map((v,n)=>n===i?{...v,duration:Number(e.target.value)}:v));setSaved(false)}}/></label>
        <button className="text-button" disabled={working} onClick={()=>{setRemotes(remotes.filter((_,n)=>n!==i));setSaved(false)}}>移除参考 {i+1}</button>
      </div>)}
      <button className="secondary" disabled={working||remotes.length>=6} onClick={()=>{setRemotes([...remotes,{kind:'video',url:'',duration:4}]);setSaved(false)}}>添加视频／音频参考</button>
      <small>提示词中可使用“图片1”“视频1”“音频1”引用对应素材，编号按各类素材顺序计算。音频须搭配图片或视频，并开启生成声音。</small>
    </>}
    {firstMode&&previous?.asset_id?<div className="continuity-controls">
      <label>上一镜头的出点帧（24 fps，从 0 开始）<input type="number" min={0} step={1}
        max={frame?frame.frame_count-1:undefined} value={frameNumber} placeholder="留空选择最后一帧"
        onChange={e=>{setFrameNumber(e.target.value);setSaved(false)}} disabled={working}/></label>
      <button className="secondary" disabled={working} onClick={prepare}>{working?<LoaderCircle className="spin" size={16}/>:null}提取并预览参考帧</button>
      <small>改帧号后需重新提取。提取只处理本地素材，不调用视频模型；保存首帧配置会同时设置上一镜头的拼接出点。</small>
    </div>:firstMode&&<p className="muted">没有已完成的上一镜头时，可上传首帧。</p>}
    {firstMode&&frame&&<div className="reference-frame">
      <img src={`/api/assets/${frame.asset_id}`} alt={frame.origin==='uploaded'?'上传的首帧':'选定的上一镜头结束帧'}/>
      <div><strong>{frame.origin==='uploaded'?`上传图片 · ${frame.width} × ${frame.height}`:`参考帧 ${frame.frame_index}`}</strong>{frame.origin!=='uploaded'&&<><p>时间 {frame.time.toFixed(3)} 秒 · 共 {frame.frame_count} 帧</p>
        <p>前镜头保留到 {(frame.out_frame_exclusive/24).toFixed(3)} 秒</p></>}
        <BackupStatus assetId={frame.asset_id} request={request}/>
        {!currentFrame&&<p className="job-error">来源视频已变化，不能继续使用此帧。</p>}
      </div>
    </div>}
    {firstMode&&frame&&frame.origin!=='uploaded'&&<div className="boundary-preview">
      <div><span>上一镜头（按出点结束）</span><video ref={firstVideo} src={`/api/assets/${frame.asset_id}/source-preview`} controls preload="auto"/></div>
      <div><span>当前镜头（已有版本）</span>{shot.asset_id?<video ref={secondVideo} src={`/api/assets/${shot.asset_id}`} controls preload="auto"/>:<p className="muted">生成后可比较开头画面</p>}</div>
      <button className="secondary" disabled={!shot.asset_id||working} onClick={previewing?stopPreview:preview}><Play size={15}/>{previewing?'停止预览':'播放切换附近'}</button>
      <small>此处顺序播放前段结尾和后段开头供人工比较，浏览器切换可能有加载间隔。最终成片按保存的出点硬切。</small>
    </div>}
    <button className="primary" disabled={working||(firstMode&&(!currentFrame||(frame?.origin!=='uploaded'&&String(frame?.frame_index)!==frameNumber)))||(mode==='first_last_frame'&&!lastId)||(mode==='reference'&&!images.length&&!remotes.length)} onClick={save}>确认生成配置</button>
  </section></div>
}
