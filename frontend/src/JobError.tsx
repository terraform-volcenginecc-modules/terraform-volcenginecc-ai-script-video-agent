import React, {useRef, useState} from 'react'
import {Copy} from 'lucide-react'
import './job-error.css'

export type ErrorDetails = {
  source?:string; summary?:string; code?:string|null; message?:string|null;
  request_id?:string|null; http_status?:number|null; provider_status?:string|null
}

type DiagnosticJob = {
  id:string; kind:string; status:string; phase:string; external_id:string|null;
  error:string|null; error_details?:ErrorDetails|null
}

export function JobError({job}: {job:DiagnosticJob}) {
  const [copied, setCopied] = useState(false)
  const [fallback, setFallback] = useState(false)
  const textRef = useRef<HTMLTextAreaElement>(null)
  const info = job.error_details
  if (!job.error && !info) return null
  const summary = info?.summary || job.error
  const diagnostic = [
    `任务 ID: ${job.id}`, `任务类型: ${job.kind}`, `任务状态: ${job.status}`,
    `任务阶段: ${job.phase}`, `方舟任务 ID: ${job.external_id || '未返回'}`,
    `说明: ${summary || '未提供'}`, `错误码: ${info?.code || '未记录 / 云端未提供'}`,
    `具体原因: ${info?.message || job.error || '未提供'}`,
    `请求 ID: ${info?.request_id || '未记录 / 云端未提供'}`,
    `HTTP 状态: ${info?.http_status ?? '未记录 / 云端未提供'}`,
    `云端状态: ${info?.provider_status || '未记录 / 云端未提供'}`,
  ].join('\n')
  async function copy() {
    try {
      await navigator.clipboard.writeText(diagnostic)
      setCopied(true)
      setFallback(false)
    } catch {
      setFallback(true)
      requestAnimationFrame(() => {textRef.current?.focus(); textRef.current?.select()})
    }
  }
  return <div className="job-diagnostics">
    <p className="job-error">{['queued','running','waiting'].includes(job.status) ? '上次尝试：' : ''}{summary}</p>
    <details>
      <summary>查看错误详情</summary>
      {info ? <dl>
        <dt>错误码</dt><dd>{info.code || '云端未提供'}</dd>
        <dt>具体原因</dt><dd>{info.message || '云端未提供具体原因'}</dd>
        <dt>请求 ID</dt><dd>{info.request_id || '云端未提供'}</dd>
        {info.http_status != null && <><dt>HTTP 状态</dt><dd>{info.http_status}</dd></>}
        {info.provider_status && <><dt>云端状态</dt><dd>{info.provider_status}</dd></>}
      </dl> : <p className="muted">此任务没有保存云端错误详情。历史任务无法还原当时未记录的信息；本地错误请检查 Worker 日志。</p>}
    </details>
    <button className="text-button" onClick={copy}><Copy size={13}/>{copied ? '再次复制诊断信息' : '复制诊断信息'}</button>
    {copied && <span className="diagnostic-copy-status" role="status">已复制</span>}
    {fallback && <label className="diagnostic-fallback">浏览器未允许复制，请选中下方内容手动复制
      <textarea ref={textRef} readOnly rows={8} value={diagnostic} onFocus={event=>event.currentTarget.select()}/>
    </label>}
  </div>
}
