import {useEffect, useState} from 'react'
import {Cloud, RefreshCw} from 'lucide-react'
import './backup.css'

export type Backup = {status:string; bucket?:string; object_key?:string; error?:string|null}
type Request = <T>(path:string, method?:string, data?:unknown)=>Promise<T>

export function BackupStatus({assetId, data, request}:{assetId:string; data?:Backup|null; request:Request}) {
  const [current,setCurrent] = useState<Backup|null>(data || null)
  const [working,setWorking] = useState(false)
  const [error,setError] = useState('')
  useEffect(()=>{
    if(data){setCurrent(data);return}
    let live=true
    async function refresh() {
      try {
        const result=await request<{backup:Backup}>(`/assets/${assetId}/info`)
        if(live)setCurrent(result.backup)
      }catch{/* The existing asset view handles missing files and API errors. */}
    }
    refresh()
    const timer=setInterval(refresh,5000)
    return ()=>{live=false;clearInterval(timer)}
  },[assetId,data,request])
  if(!current || current.status==='disabled')return null
  const label = {queued:'等待云端备份',uploading:'正在备份到云端',succeeded:'已备份到云端',failed:'云端备份失败'}[current.status] || '云端备份处理中'
  async function retry(){
    setWorking(true);setError('')
    try{setCurrent(await request<Backup>(`/assets/${assetId}/backup/retry`,'POST'))}
    catch(e){setError((e as Error).message)}
    finally{setWorking(false)}
  }
  return <div className={'backup-status '+current.status} title={current.bucket ? `${current.bucket}/${current.object_key || ''}` : undefined}>
    <span><Cloud size={13}/>{label}</span>
    {current.status==='failed'&&<>
      <small>{current.error}；本地素材仍可播放，后台会自动重试。</small>
      <button className="text-button" disabled={working} onClick={retry}><RefreshCw size={12}/>重试上传</button>
    </>}
    {error&&<small role="alert">{error}</small>}
  </div>
}
