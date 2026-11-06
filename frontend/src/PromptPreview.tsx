import {useEffect, useState} from 'react'
import {X} from 'lucide-react'

type Preview = {prompt:string;model:string;duration:number;ratio:string;resolution:string;generate_audio:boolean;watermark:boolean;execution_expires_after:number;return_last_frame:boolean;reference_asset_id:string|null;references:{asset_id?:string;role:string;type:string;url?:string;duration?:number}[];warnings:string[]}
export function PromptPreview({projectId,shotId,request,onClose}:{
  projectId:string;shotId:string;request:<T>(path:string)=>Promise<T>;onClose:()=>void
}) {
  const [data,setData]=useState<Preview|null>(null)
  const [error,setError]=useState('')
  useEffect(()=>{
    let live=true
    request<Preview>(`/projects/${projectId}/shots/${shotId}/generation-preview`)
      .then(value=>{if(live)setData(value)}).catch(e=>{if(live)setError(e.message)})
    return ()=>{live=false}
  },[projectId,shotId])
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label="实际提示词预览">
    <div className="section-heading"><h2>实际提示词与生成规格</h2><button className="icon-button" onClick={onClose} aria-label="关闭"><X/></button></div>
    <p className="muted">展示当前已保存分镜的实际提交文本。预览不调用视频模型；修改分镜后可重新预览。</p>
    {error&&<div className="alert" role="alert">{error}</div>}
    {data?<><p>{data.model} · {data.resolution} · {data.ratio} · {data.duration} 秒 · {data.generate_audio?'有声':'无声'} · {data.watermark?'带水印':'无水印'}</p>
      <p className="muted">云端超时 {data.execution_expires_after/3600} 小时</p>
      {data.references.map((r,i)=><div key={i}><p>{{first_frame:'首帧',last_frame:'尾帧',reference_image:'参考图片',reference_video:'参考视频',reference_audio:'参考音频'}[r.role]} {r.duration&&`· ${r.duration} 秒`}</p>{r.asset_id?<img src={`/api/assets/${r.asset_id}`} alt={`生成参考 ${i+1}`} style={{width:'100%',maxHeight:200,objectFit:'contain'}}/>:<p style={{overflowWrap:'anywhere'}}>{r.url}</p>}</div>)}
      {data.warnings.map(w=><p className="key-note" key={w}>{w}</p>)}
      <label>实际提示词（{data.prompt.length} 字符）<textarea rows={14} readOnly value={data.prompt}/></label>
    </>:!error&&<p>正在读取已保存配置…</p>}
  </section></div>
}
