import {forwardRef, useCallback, useEffect, useRef, useState, type ImgHTMLAttributes, type ReactNode, type VideoHTMLAttributes} from 'react'

type Access = {status:string;url:string|null;expires_at:string|null;error?:string}

async function accessFor(assetId:string, preview=false, download=false, signal?:AbortSignal):Promise<Access> {
  const response = await fetch(`/api/assets/${assetId}/access?preview=${preview}&download=${download}`, {
    credentials:'same-origin', cache:'no-store', signal,
  })
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'读取素材地址失败')
  return data
}

function useMedia(assetId:string, preview=false, beforeChange?:()=>void) {
  const [access,setAccess] = useState<Access|null>(null)
  const [error,setError] = useState('')
  const [version,setVersion] = useState(0)
  const failures = useRef(0)
  const previous = useRef('')
  const onChange = useRef(beforeChange)
  onChange.current = beforeChange
  const refresh = useCallback(()=>{
    if(failures.current){setAccess(null);previous.current=''}
    failures.current=0;setError('');setVersion(v=>v+1)
  },[])
  const failed = useCallback(()=>{
    if (failures.current++ === 0) {setAccess(null);previous.current='';setVersion(v=>v+1)}
    else setError('素材加载失败，请刷新地址后重试')
  },[])
  useEffect(()=>{
    setAccess(null);setError('');failures.current=0;previous.current=''
  },[assetId,preview])
  useEffect(()=>{
    const controller = new AbortController()
    let timer:ReturnType<typeof setTimeout>
    async function load() {
      let delay = 5000
      try {
        const value = await accessFor(assetId,preview,false,controller.signal)
        if (controller.signal.aborted) return
        if (value.url !== previous.current) onChange.current?.()
        previous.current = value.url || ''
        setAccess(value);setError('')
        if (value.url) {
          if (!value.expires_at) return
          const remaining = new Date(value.expires_at).getTime()-Date.now()
          delay = Math.max(1000, remaining-Math.min(60000,remaining/3))
        }
      } catch (e) {
        if (controller.signal.aborted) return
        setError((e as Error).message)
      }
      timer = setTimeout(load,delay)
    }
    load()
    const visible = ()=>{if(document.visibilityState==='visible')refresh()}
    document.addEventListener('visibilitychange',visible)
    return ()=>{controller.abort();clearTimeout(timer);document.removeEventListener('visibilitychange',visible)}
  },[assetId,preview,version,refresh])
  const message = error || access?.error || (
    access?.status==='failed'?'素材上传失败，可重试上传':
    access?.status==='uploading'?'素材正在上传':
    access?.status==='queued'?'等待素材上传':'正在读取素材')
  return {url:access?.url || undefined,error,message,refresh,failed}
}

function MediaMessage({message,refresh}:{message:string;refresh:()=>void}) {
  return <div className="media-message" role="status"><span>{message}</span><button className="text-button" onClick={refresh}>刷新地址</button></div>
}

export const MediaVideo = forwardRef<HTMLVideoElement, VideoHTMLAttributes<HTMLVideoElement>&{assetId:string;preview?:boolean}>(
  function MediaVideo({assetId,preview=false,onLoadedMetadata,...props},ref) {
    const video = useRef<HTMLVideoElement|null>(null)
    const playback = useRef<{time:number;playing:boolean}|null>(null)
    useEffect(()=>{playback.current=null},[assetId,preview])
    const media = useMedia(assetId,preview,()=>{
      if (video.current) playback.current={time:video.current.currentTime,playing:!video.current.paused}
    })
    return <>{media.url&&<video {...props} ref={element=>{
      video.current=element
      if(typeof ref==='function')ref(element)
      else if(ref)ref.current=element
    }} src={media.url} onError={media.failed} onLoadedMetadata={e=>{
      const saved=playback.current
      playback.current=null
      if(saved) {
        e.currentTarget.currentTime=Math.min(saved.time,Math.max(0,e.currentTarget.duration-.01))
        if(saved.playing)e.currentTarget.play().catch(()=>{})
      }
      onLoadedMetadata?.(e)
    }}/>}
    {(!media.url||media.error)&&<MediaMessage message={media.message} refresh={media.refresh}/>}</>
  },
)

export function MediaImage({assetId,...props}:ImgHTMLAttributes<HTMLImageElement>&{assetId:string}) {
  const media=useMedia(assetId)
  return <>{media.url&&<img {...props} src={media.url} onError={media.failed}/>}
    {(!media.url||media.error)&&<MediaMessage message={media.message} refresh={media.refresh}/>}</>
}

export function MediaDownload({assetId,children}:{assetId:string;children:ReactNode}) {
  const [working,setWorking]=useState(false)
  const [error,setError]=useState('')
  async function download() {
    setWorking(true);setError('')
    try {
      const access=await accessFor(assetId,false,true)
      if(!access.url)throw new Error('素材尚未上传完成，请稍后重试')
      window.location.assign(access.url)
    } catch(e){setError((e as Error).message)}
    finally{setWorking(false)}
  }
  return <span><button className="primary" disabled={working} onClick={download}>{children}</button>
    {error&&<small role="alert">{error}</small>}</span>
}
