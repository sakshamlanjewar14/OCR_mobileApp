import React, { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, Image, Linking, SafeAreaView, ScrollView, StatusBar, StyleSheet, Switch, Text, TextInput, TouchableOpacity, View } from 'react-native';
import { Camera, CameraView, useCameraPermissions, type BarcodeScanningResult } from 'expo-camera';
import * as ImagePicker from 'expo-image-picker';
import * as FileSystem from 'expo-file-system/legacy';
import * as Sharing from 'expo-sharing';
import { parseQrPayload } from './qr.js';
import { fieldsFromLabeledText, replaceLabeledLine } from './draft.js';

const API = process.env.EXPO_PUBLIC_API_URL || 'http://127.0.0.1:8001/api';
type Fields = { company_name: string; owner_name: string; designation: string; business_description: string; company_address: string; contact_numbers: string[]; email_addresses: string[]; website: string; facebook_link: string; instagram_link: string; whatsapp_link: string; raw_text: string };
type Lead = Fields & { id: string; lead_number: string; verification_status: string; source_document_ids: string[]; document_id: string | null; original_ocr_text: string | null };
type Side = { uri: string; name: string; side: 'front' | 'back' };
type Doc = { id: string; processing_status: string; error_message?: string | null; extracted_fields: Record<string, any> };
const empty: Fields = { company_name: '', owner_name: '', designation: '', business_description: '', company_address: '', contact_numbers: [], email_addresses: [], website: '', facebook_link: '', instagram_link: '', whatsapp_link: '', raw_text: '' };
const fieldRows: [keyof Fields, string][] = [['company_name','Full company name'],['owner_name','Owner / contact name'],['designation','Designation'],['business_description','Business description / services'],['company_address','Address'],['website','Website'],['facebook_link','Facebook link'],['instagram_link','Instagram link'],['whatsapp_link','WhatsApp link']];
const getText = (v: unknown) => typeof v === 'string' ? v : '';
const values = (a: unknown, fallback?: unknown) => Array.isArray(a) ? a.map(String).filter(Boolean) : fallback ? [String(fallback)] : [];
const headers: Partial<Record<keyof Fields, string>> = { company_name:'Company name', owner_name:'Owner name', designation:'Designation', business_description:'Business description', company_address:'Address', website:'Website', facebook_link:'Facebook', instagram_link:'Instagram', whatsapp_link:'WhatsApp' };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!__DEV__ && API.startsWith('http://')) throw new Error('Release builds require an HTTPS API URL.');
  let response: Response;
  try { response = await fetch(`${API}${path}`, init); } catch { throw new Error('Cannot reach Lead Gennie API. Keep this draft, check the API address and network, then retry.'); }
  if (!response.ok) { let message = `Request failed (${response.status}).`; try { message = (await response.json()).detail || message; } catch {} throw new Error(message); }
  return response.status === 204 ? undefined as T : response.json();
}
function docFields(doc: Doc): Partial<Fields> {
  const ex = doc.extracted_fields || {};
  const candidate = Array.isArray(ex.cards) && ex.cards.length ? ex.cards[0] : ex;
  return { company_name:getText(candidate.company_name), owner_name:getText(candidate.owner_name), designation:getText(candidate.designation), business_description:getText(candidate.business_description), company_address:getText(candidate.company_address), contact_numbers:values(candidate.contact_numbers,candidate.contact_number), email_addresses:values(candidate.email_addresses,candidate.email), website:getText(candidate.website), facebook_link:getText(candidate.facebook_link), instagram_link:getText(candidate.instagram_link), whatsapp_link:getText(candidate.whatsapp_link), raw_text:getText(candidate.raw_text || ex.raw_text) };
}
function mergeDraft(docs: Doc[]): { form: Fields; conflicts: string[] } {
  const parsed = docs.map(docFields); const conflicts: string[] = [];
  const form = { ...empty } as Fields;
  for (const key of Object.keys(headers) as (keyof typeof headers)[]) {
    const candidates = parsed.map(row => getText(row[key])).filter(Boolean);
    if (new Set(candidates.map(x => x.trim().toLocaleLowerCase())).size > 1) conflicts.push(`${headers[key]} differs between front and back. Check the card and edit the value.`);
    form[key as keyof Fields] = (candidates[0] || '') as never;
  }
  form.contact_numbers = [...new Set(parsed.flatMap(row => row.contact_numbers || []))];
  form.email_addresses = [...new Set(parsed.flatMap(row => row.email_addresses || []))];
  form.raw_text = docs.map((doc, i) => `--- ${i ? 'Back' : 'Front'} ---\n${getText(doc.extracted_fields?.raw_text)}`).filter(Boolean).join('\n\n');
  return { form, conflicts };
}
function fieldLabel(key: keyof Fields) { return headers[key] || key; }
function dedupe(a: string[], b: unknown) { return [...new Set([...a, ...values(b)])]; }

export default function App() {
  const [permission, requestPermission] = useCameraPermissions(); const camera = useRef<CameraView>(null);
  const [leads, setLeads] = useState<Lead[]>([]); const [leadTotal, setLeadTotal] = useState(0); const [leadsPage, setLeadsPage] = useState(1); const [page, setPage] = useState<'leads'|'capture'|'review'|'details'>('leads');
  const [sides, setSides] = useState<Side[]>([]); const [qrPayloads, setQrPayloads] = useState<string[]>([]); const [form, setForm] = useState<Fields>(empty); const [conflicts, setConflicts] = useState<string[]>([]);
  const [originalOcrText, setOriginalOcrText] = useState(''); const [translationWarning, setTranslationWarning] = useState('');
  const [filters, setFilters] = useState({ search:'', company_name:'', owner_name:'', state:'', city:'' }); const [selectedIds, setSelectedIds] = useState<string[]>([]); const [exportCount, setExportCount] = useState('');
  const [sourceIds, setSourceIds] = useState<string[]>([]); const [previewLead, setPreviewLead] = useState<Lead|null>(null); const [editingId, setEditingId] = useState<string|null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [cameraOn, setCameraOn] = useState(false);

  const queryForFilters = (query:URLSearchParams, criteria:typeof filters) => { for(const key of ['search','company_name','owner_name','state','city'] as const) if(criteria[key].trim())query.set(key,criteria[key].trim()); };
  const loadLeads = async (criteria=filters) => { try { const query=new URLSearchParams({page:'1',page_size:'100'});queryForFilters(query,criteria);const result = await request<{leads:Lead[];total:number}>('/leads?'+query.toString()); setLeads(result.leads);setLeadTotal(result.total);setLeadsPage(1); } catch (e) { setError(e instanceof Error ? e.message : 'Could not load leads.'); } };
  const loadMoreLeads = async () => { try { const next=leadsPage+1;const query=new URLSearchParams({page:String(next),page_size:'100'});queryForFilters(query,filters);const result=await request<{leads:Lead[]}>('/leads?'+query.toString());setLeads(current=>[...current,...result.leads]);setLeadsPage(next); } catch(e){setError(e instanceof Error?e.message:'Could not load more leads.');} };
  useEffect(() => { void loadLeads(); }, []);
  const visibleLeads = leads;
  const exportLeads = async () => {
    try {
      const query = new URLSearchParams();
      if (selectedIds.length) selectedIds.forEach(id=>query.append('ids',id));
      else queryForFilters(query,filters);
      if(exportCount.trim()){const count=Number(exportCount);if(!Number.isInteger(count)||count<1)throw new Error('Rows to export must be a positive whole number.');query.set('export_count',String(count));}
      const target=`${API}/exports/leads?${query.toString()}`;
      const path=`${FileSystem.cacheDirectory}lead-gennie-${Date.now()}.xlsx`;
      const download=await FileSystem.downloadAsync(target,path);
      if(await Sharing.isAvailableAsync()) await Sharing.shareAsync(download.uri,{mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',dialogTitle:'Share Lead Gennie leads'});
      else await Linking.openURL(download.uri);
      setNotice(`Exported ${selectedIds.length?selectedIds.length:visibleLeads.length} lead record(s).`);
    } catch(e) {setError(e instanceof Error?e.message:'Excel export failed.');}
  };
  const toggleSelect = (id:string) => setSelectedIds(current=>current.includes(id)?current.filter(value=>value!==id):[...current,id]);
  const putSide = (side: 'front'|'back', asset: { uri: string; fileName?: string | null }) => {
    const next: Side = { uri: asset.uri, name: asset.fileName || `${side}-card.jpg`, side };
    setSides(current => [...current.filter(item => item.side !== side), next].sort((a,b) => a.side === 'front' ? -1 : b.side === 'front' ? 1 : 0));
  };
  const chooseImage = async (side: 'front'|'back') => {
    const result = await ImagePicker.launchImageLibraryAsync({ mediaTypes:['images'], quality:0.9 });
    if (!result.canceled && result.assets[0]) {
      putSide(side, result.assets[0]); setError('');
      try { const codes = await Camera.scanFromURLAsync(result.assets[0].uri, ['qr']); setQrPayloads(current => [...new Set([...current, ...codes.map(code=>code.data)])]); }
      catch { /* Image decoding may be unavailable for a given URI; OCR and manual QR entry remain available. */ }
    }
  };
  const takePhoto = async () => {
    if (!camera.current) return;
    try { const photo = await camera.current.takePictureAsync({ quality:0.9 }); if (photo) { const side=sides.some(x=>x.side==='front') ? 'back' : 'front'; putSide(side, { uri:photo.uri, fileName:`card-${Date.now()}.jpg` }); try { const codes=await Camera.scanFromURLAsync(photo.uri,['qr']); setQrPayloads(current=>[...new Set([...current,...codes.map(code=>code.data)])]); } catch {} setCameraOn(false); } }
    catch { setError('Camera could not capture this image. Try again or choose a photo.'); }
  };
  const onBarcode = (result: BarcodeScanningResult) => { setQrPayloads(current => current.includes(result.data) ? current : [...current,result.data]); };
  const processCard = async () => {
    if (!sides.some(x=>x.side==='front')) { setError('Add a front image first.'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const data = new FormData();
      for (const side of sides) data.append('files', { uri:side.uri, name:side.name, type:'image/jpeg' } as any);
      const uploaded = await request<{documents:Doc[]}>('/documents/batch', { method:'POST', body:data });
      const docs = uploaded.documents;
      for (let attempt=0; attempt<100; attempt++) {
        const statuses = await request<{id:string;processing_status:string;error_message:string|null}[]>('/documents/status', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:docs.map(d=>d.id)}) });
        if (statuses.every(x=>['needs_review','failed','saved'].includes(x.processing_status))) break;
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
      const ready = await Promise.all(docs.map(doc=>request<Doc>(`/documents/${doc.id}`)));
      const failed = ready.find(doc=>doc.processing_status==='failed'); if (failed) throw new Error(failed.error_message || 'OCR could not read one side. You can still review by entering the details manually.');
      const merged = mergeDraft(ready); let draft = merged.form; const qrConflicts: string[] = [...merged.conflicts];
      setOriginalOcrText(ready.map((doc,index)=>`--- ${index?'Back':'Front'} original OCR ---\n${getText(doc.extracted_fields?.original_ocr_text || doc.extracted_fields?.raw_text)}`).join('\n\n'));
      setTranslationWarning(ready.map(doc=>getText(doc.extracted_fields?.translation_message)).filter(Boolean).join('\n'));
      for (const payload of qrPayloads) {
        const parsed = parseQrPayload(payload);
        const qFields: Partial<Fields> = { company_name:parsed.company_name, owner_name:parsed.owner_name, designation:parsed.designation, company_address:parsed.company_address, website:parsed.website, facebook_link:parsed.facebook_link, instagram_link:parsed.instagram_link, whatsapp_link:parsed.whatsapp_link, contact_numbers:values(parsed.contacts), email_addresses:values(parsed.emails) };
        for (const key of Object.keys(headers) as (keyof Fields)[]) { const q=getText(qFields[key]); if (!q) continue; const current=getText(draft[key]); if(current && current.toLowerCase()!==q.toLowerCase()) qrConflicts.push(`${fieldLabel(key)} conflicts with QR data. Compare both sources.`); else draft[key]=q as never; }
        draft.contact_numbers=dedupe(draft.contact_numbers,parsed.contacts); draft.email_addresses=dedupe(draft.email_addresses,parsed.emails);
        if (parsed.location_coordinates) { draft.raw_text += `\nQR coordinates: ${parsed.location_coordinates.latitude}, ${parsed.location_coordinates.longitude}`; }
        if (parsed.map_url) draft.raw_text += `\nMap QR URL: ${parsed.map_url}`;
        draft.raw_text += `\nQR source payload: ${payload}`;
      }
      setSourceIds(ready.map(doc=>doc.id)); setForm(draft); setConflicts(qrConflicts); setEditingId(null); setPage('review'); setNotice('Review the extracted values and source images before saving.');
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not process the card. Your images remain selected.'); }
    finally { setBusy(false); }
  };
  const setText = (key: keyof Fields, text: string) => setForm(old=>key==='raw_text'?fieldsFromLabeledText(text,{...old,raw_text:text}):{...old,[key]:text,raw_text:replaceLabeledLine(old.raw_text,key,text)});
  const setListText = (key:'contact_numbers'|'email_addresses',text:string) => {const parsed=text.split(/[\n;,]+/).map(x=>x.trim()).filter(Boolean);setForm(old=>({...old,[key]:parsed,raw_text:replaceLabeledLine(old.raw_text,key,parsed.join('; '))}));};
  const save = async () => {
    if (!form.company_name.trim()) { setError('Company name is required.'); return; }
    setBusy(true); setError('');
    const payload = { ...form, company_name:form.company_name.trim(), owner_name:form.owner_name||null, designation:form.designation||null, company_address:form.company_address||null, website:form.website||null, facebook_link:form.facebook_link||null, instagram_link:form.instagram_link||null, whatsapp_link:form.whatsapp_link||null, contact_numbers:form.contact_numbers, contact_number:form.contact_numbers[0]||null, email_addresses:form.email_addresses, email:form.email_addresses[0]||null, raw_text:form.raw_text||null, original_ocr_text:originalOcrText||null, document_id:sourceIds[0]||null, source_document_ids:sourceIds, source_sides:sides.map(x=>x.side), source_review_confirmed:true };
    try {
      if (editingId) await request(`/leads/${editingId}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      else await request('/leads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      setNotice(editingId?'Lead updated.':'Lead saved.'); setPage('leads'); setSides([]); setSourceIds([]); setQrPayloads([]); setEditingId(null); await loadLeads();
    } catch(e) { setError(e instanceof Error ? e.message : 'Save failed. Your draft remains available; retry when connected.'); }
    finally { setBusy(false); }
  };
  const edit = (lead:Lead) => { setPreviewLead(null); setOriginalOcrText(lead.original_ocr_text||''); setForm({ company_name:lead.company_name, owner_name:lead.owner_name||'', designation:lead.designation||'', business_description:lead.business_description||'', company_address:lead.company_address||'', contact_numbers:lead.contact_numbers||[], email_addresses:lead.email_addresses||[], website:lead.website||'', facebook_link:lead.facebook_link||'', instagram_link:lead.instagram_link||'', whatsapp_link:lead.whatsapp_link||'', raw_text:lead.raw_text||'' }); setEditingId(lead.id); setPage('review'); };
  const openLink = async (url: string) => { const normalized=/^[a-z]+:/i.test(url)?url:`https://${url}`; try { await Linking.openURL(normalized); } catch { setError('No app is available to open this link.'); } };
  const remove = (lead:Lead) => Alert.alert('Delete lead?', `Delete ${lead.company_name}?`, [{text:'Cancel',style:'cancel'},{text:'Delete',style:'destructive',onPress:async()=>{try{await request(`/leads/${lead.id}`,{method:'DELETE'}); await loadLeads();}catch(e){setError(e instanceof Error?e.message:'Could not delete lead.');}}}]);
  const resolveLocation = async () => {
    const found = form.raw_text.match(/QR coordinates:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)/); if(!found) return;
    setBusy(true); try { const result=await request<{address:string;attribution:string}>('/geocoding/reverse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude:Number(found[1]),longitude:Number(found[2])})}); setText('company_address',result.address); setNotice(result.attribution); }
    catch(e){setError(e instanceof Error?e.message:'Address lookup unavailable. Enter address manually.');} finally{setBusy(false);}
  };

  return <SafeAreaView style={styles.safe}><StatusBar barStyle="light-content" backgroundColor="#124532"/><View style={styles.header}><View style={styles.logo}><Text style={styles.logoText}>L</Text></View><View><Text style={styles.brand}>Lead Gennie</Text><Text style={styles.tagline}>Business leads, ready to review</Text></View></View>
    <View style={styles.nav}><TouchableOpacity onPress={()=>{setPage('leads');void loadLeads();}}><Text style={[styles.navText,page==='leads'||page==='details'?styles.navActive:null]}>Leads ({leadTotal})</Text></TouchableOpacity><TouchableOpacity onPress={()=>{setSides([]);setQrPayloads([]);setError('');setPage('capture');}}><Text style={[styles.navText,page==='capture'?styles.navActive:null]}>Scan card</Text></TouchableOpacity></View>
    <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      {!!error&&<Text style={styles.error}>{error}</Text>}{!!notice&&<Text style={styles.notice}>{notice}</Text>}
      {page==='leads'&&<><Text style={styles.title}>Your leads</Text><TouchableOpacity style={styles.primary} onPress={()=>{setSides([]);setQrPayloads([]);setPage('capture');}}><Text style={styles.primaryText}>＋ Scan a business card</Text></TouchableOpacity><Field label="Search leads" value={filters.search} onChange={value=>setFilters(f=>({...f,search:value}))}/><View style={styles.filterGrid}>{(['company_name','owner_name','state','city'] as const).map(key=><Field key={key} label={key==='company_name'?'Company name':key==='owner_name'?'Owner name':key==='state'?'State (in address)':'City (in address)'} value={filters[key]} onChange={value=>setFilters(f=>({...f,[key]:value}))}/>)}</View><View style={styles.row}><TouchableOpacity style={styles.secondary} onPress={()=>void loadLeads()}><Text style={styles.secondaryText}>Apply filters</Text></TouchableOpacity><TouchableOpacity style={styles.secondary} onPress={()=>{setSelectedIds(visibleLeads.map(lead=>lead.id));}}><Text style={styles.secondaryText}>Select filtered</Text></TouchableOpacity></View><View style={styles.row}><Field label="Rows to export (optional)" value={exportCount} onChange={setExportCount}/><TouchableOpacity style={styles.secondary} onPress={()=>void exportLeads()} disabled={!visibleLeads.length}><Text style={styles.secondaryText}>Export & share Excel {selectedIds.length?`(${selectedIds.length})`:''}</Text></TouchableOpacity></View>{leads.length===0?<Text style={styles.empty}>{leadTotal?'No leads match these filters.':'No saved leads yet. Scan a card and review the details before saving.'}</Text>:visibleLeads.map((lead,i)=><TouchableOpacity key={lead.id} style={styles.card} onPress={()=>{setPreviewLead(lead);setPage('details');}}><View style={styles.actionLine}><Text style={styles.serial}>S.No. {i+1} · {lead.lead_number}</Text><View style={styles.row}><Text style={styles.detailHint}>Select</Text><Switch value={selectedIds.includes(lead.id)} onValueChange={()=>toggleSelect(lead.id)} /></View></View><Text style={styles.cardTitle}>{lead.company_name}</Text><Text style={styles.line}>{lead.owner_name||'No owner listed'}{lead.designation?` · ${lead.designation}`:''}</Text>{lead.contact_numbers?.map((n,j)=><View key={`${n}-${j}`} style={styles.actionLine}><Text style={styles.line}>{n}</Text><TouchableOpacity style={styles.smallButton} onPress={()=>void openLink(`tel:${n.replace(/\s/g,'')}`)}><Text style={styles.smallButtonText}>Call</Text></TouchableOpacity></View>)}{lead.email_addresses?.map((email,j)=><TouchableOpacity key={`${email}-${j}`} onPress={()=>void openLink(`mailto:${email}`)}><Text style={styles.link}>{email}</Text></TouchableOpacity>)}{lead.website&&<TouchableOpacity onPress={()=>void openLink(lead.website!)}><Text style={styles.link}>{lead.website}</Text></TouchableOpacity>}<Text style={styles.detailHint}>Tap to view details · Edit · Delete</Text></TouchableOpacity>)}{leads.length<leadTotal&&<TouchableOpacity style={styles.secondary} onPress={()=>void loadMoreLeads()}><Text style={styles.secondaryText}>Load more leads ({leads.length} of {leadTotal})</Text></TouchableOpacity>}</>}
      {page==='capture'&&<><Text style={styles.title}>Scan a business card</Text><Text style={styles.sub}>Capture or choose the front. The back is optional. Both sides become one lead.</Text>{cameraOn?<View style={styles.cameraBox}>{permission?.granted?<CameraView ref={camera} style={styles.camera} facing="back" onBarcodeScanned={onBarcode} barcodeScannerSettings={{barcodeTypes:['qr']}}/>:<View style={styles.permission}><Text style={styles.line}>Camera permission is needed to scan.</Text><TouchableOpacity style={styles.primary} onPress={()=>void requestPermission()}><Text style={styles.primaryText}>Allow camera</Text></TouchableOpacity></View>}<TouchableOpacity style={styles.secondary} onPress={()=>setCameraOn(false)}><Text style={styles.secondaryText}>Close camera</Text></TouchableOpacity></View>:<TouchableOpacity style={styles.secondary} onPress={async()=>{if(!permission?.granted){const p=await requestPermission();if(!p.granted)return;}setCameraOn(true);}}><Text style={styles.secondaryText}>Open scanner camera (QR is detected as you scan)</Text></TouchableOpacity>}
        <View style={styles.row}><TouchableOpacity style={styles.secondary} onPress={()=>void takePhoto()}><Text style={styles.secondaryText}>Capture next side</Text></TouchableOpacity><TouchableOpacity style={styles.secondary} onPress={()=>void chooseImage(sides.some(x=>x.side==='front')?'back':'front')}><Text style={styles.secondaryText}>Choose front/back image</Text></TouchableOpacity></View>
        {(['front','back'] as const).map(side=>{const image=sides.find(x=>x.side===side);return <View key={side} style={styles.card}><Text style={styles.label}>{side==='front'?'FRONT SIDE':'BACK SIDE'} {side==='back'?'(OPTIONAL)':''}</Text>{image?<><Image source={{uri:image.uri}} style={styles.preview}/><View style={styles.row}><TouchableOpacity style={styles.secondary} onPress={()=>void chooseImage(side)}><Text style={styles.secondaryText}>Retake / replace</Text></TouchableOpacity><TouchableOpacity style={styles.secondary} onPress={()=>setSides(x=>x.filter(s=>s.side!==side))}><Text style={styles.secondaryText}>Remove</Text></TouchableOpacity></View></>:<Text style={styles.line}>{side==='front'?'Required':'Add the back if it has more details.'}</Text>}</View>;})}
        {qrPayloads.length>0&&<Text style={styles.notice}>{qrPayloads.length} QR code(s) detected. Values will be checked in the review.</Text>}<TouchableOpacity style={styles.primary} disabled={busy||!sides.some(x=>x.side==='front')} onPress={()=>void processCard()}>{busy?<ActivityIndicator color="#fff"/>:<Text style={styles.primaryText}>Read card and review</Text>}</TouchableOpacity></>}
      {page==='review'&&<><Text style={styles.title}>{editingId?'Edit lead':'Review lead'}</Text><Text style={styles.sub}>Correct the extracted details. Nothing is saved until you tap Save lead.</Text>{sides.map(side=><View style={styles.card} key={side.side}><Text style={styles.label}>{side.side.toUpperCase()} SOURCE</Text><Image style={styles.preview} source={{uri:side.uri}}/></View>)}{!!translationWarning&&<Text style={styles.warning}>{translationWarning}</Text>}{sourceIds.length>0&&<View style={styles.card}>{sourceIds.map((id,index)=><View key={id}><Text style={styles.label}>{index?'BACK':'FRONT'} OCR SOURCE</Text></View>)}<Text style={styles.sourceText}>{originalOcrText||'Original OCR text is not available.'}</Text></View>}{conflicts.map((message,index)=><Text style={styles.warning} key={index}>Review conflict: {message}</Text>)}
        {fieldRows.map(([key,label])=><Field key={key} label={label} value={form[key] as string} onChange={value=>setText(key,value)} multiline={key==='company_address'||key==='business_description'}/>)}
        <Field label="Contact numbers · one per line" value={form.contact_numbers.join('\n')} onChange={value=>setListText('contact_numbers',value)} multiline/><Field label="Email addresses · one per line" value={form.email_addresses.join('\n')} onChange={value=>setListText('email_addresses',value)} multiline/><Field label="Other document text" value={form.raw_text} onChange={value=>setText('raw_text',value)} multiline/>
        {form.raw_text.includes('QR coordinates:')&&<><TouchableOpacity style={styles.secondary} onPress={()=>void resolveLocation()}><Text style={styles.secondaryText}>Resolve address from QR coordinates</Text></TouchableOpacity><Text style={styles.attribution}>Address lookup sends coordinates only to the configured Nominatim service. © OpenStreetMap contributors</Text></>}
        <View style={styles.row}><TouchableOpacity style={styles.secondary} onPress={()=>setPage(editingId?'details':'capture')}><Text style={styles.secondaryText}>Back</Text></TouchableOpacity><TouchableOpacity style={styles.primary} disabled={busy} onPress={()=>void save()}>{busy?<ActivityIndicator color="#fff"/>:<Text style={styles.primaryText}>{editingId?'Save changes':'Save lead'}</Text>}</TouchableOpacity></View></>}
      {page==='details'&&previewLead&&<><Text style={styles.title}>{previewLead.company_name}</Text><Text style={styles.serial}>{previewLead.lead_number} · {previewLead.verification_status==='verified'?'Checked':'Review needed'}</Text>{[['Owner',previewLead.owner_name],['Designation',previewLead.designation],['Address',previewLead.company_address],['Website',previewLead.website],['Facebook',previewLead.facebook_link],['Instagram',previewLead.instagram_link],['WhatsApp',previewLead.whatsapp_link]].map(([label,value])=><View key={label}><Text style={styles.label}>{label}</Text>{value?<TouchableOpacity onPress={()=>void openLink(String(value))}><Text style={styles.link}>{value}</Text></TouchableOpacity>:<Text style={styles.line}>—</Text>}</View>)}<Text style={styles.label}>Business description / services</Text><Text style={styles.line}>{previewLead.business_description||'—'}</Text><Text style={styles.label}>Contact numbers</Text>{previewLead.contact_numbers?.map((n,i)=><View style={styles.actionLine} key={i}><Text style={styles.line}>{n}</Text><TouchableOpacity style={styles.smallButton} onPress={()=>void openLink(`tel:${n.replace(/\s/g,'')}`)}><Text style={styles.smallButtonText}>Call</Text></TouchableOpacity></View>)}<Text style={styles.label}>Email addresses</Text>{previewLead.email_addresses?.map((email,i)=><TouchableOpacity key={i} onPress={()=>void openLink(`mailto:${email}`)}><Text style={styles.link}>{email}</Text></TouchableOpacity>)}<Text style={styles.label}>Source images</Text>{previewLead.source_document_ids?.map((id,i)=><Image key={id} source={{uri:`${API}/documents/${id}/file`}} style={styles.preview}/ >)}{previewLead.raw_text&&<><Text style={styles.label}>Other document text</Text><Text style={styles.sourceText}>{previewLead.raw_text}</Text></>}{previewLead.original_ocr_text&&<><Text style={styles.label}>Original OCR transcript</Text><Text style={styles.sourceText}>{previewLead.original_ocr_text}</Text></>}<View style={styles.row}><TouchableOpacity style={styles.secondary} onPress={()=>edit(previewLead)}><Text style={styles.secondaryText}>Edit lead</Text></TouchableOpacity><TouchableOpacity style={styles.secondary} onPress={()=>remove(previewLead)}><Text style={styles.dangerText}>Delete</Text></TouchableOpacity></View></>}
    </ScrollView></SafeAreaView>;
}

function Field({label,value,onChange,multiline=false}:{label:string;value:string;onChange:(v:string)=>void;multiline?:boolean}) { return <View style={styles.field}><Text style={styles.label}>{label}</Text><TextInput style={[styles.input,multiline&&styles.multiline]} value={value} onChangeText={onChange} multiline={multiline} autoCapitalize="none" textAlignVertical={multiline?'top':'center'}/></View>; }

const styles=StyleSheet.create({safe:{flex:1,backgroundColor:'#f2f6f3'},header:{height:72,backgroundColor:'#124532',flexDirection:'row',alignItems:'center',paddingHorizontal:18,gap:12},logo:{width:40,height:40,borderRadius:12,backgroundColor:'#d4eee0',alignItems:'center',justifyContent:'center'},logoText:{fontSize:22,fontWeight:'900',color:'#124532'},brand:{color:'#fff',fontSize:19,fontWeight:'800'},tagline:{color:'#d4eee0',fontSize:12},nav:{height:48,backgroundColor:'#fff',flexDirection:'row',alignItems:'center',justifyContent:'space-around',borderBottomWidth:1,borderColor:'#dce7df'},navText:{color:'#547064',fontWeight:'700',padding:12},navActive:{color:'#126243',borderBottomWidth:3,borderColor:'#168154'},content:{padding:16,paddingBottom:40,gap:12},title:{fontSize:27,fontWeight:'800',color:'#173a2e',marginTop:4},sub:{color:'#53675d',fontSize:15,lineHeight:22},primary:{backgroundColor:'#146544',padding:14,borderRadius:11,alignItems:'center',justifyContent:'center',minHeight:48,flexGrow:1},primaryText:{color:'#fff',fontWeight:'800',fontSize:15,textAlign:'center'},secondary:{borderWidth:1,borderColor:'#c7d8ce',backgroundColor:'#fff',padding:12,borderRadius:10,alignItems:'center',justifyContent:'center',flexGrow:1},secondaryText:{color:'#20543d',fontWeight:'700',textAlign:'center'},card:{backgroundColor:'#fff',borderWidth:1,borderColor:'#dce7df',padding:14,borderRadius:14,gap:7},serial:{fontSize:12,color:'#687c70',fontWeight:'700'},cardTitle:{fontSize:19,color:'#173a2e',fontWeight:'800'},line:{fontSize:14,color:'#344a3e',lineHeight:21},link:{fontSize:14,color:'#075da5',textDecorationLine:'underline',lineHeight:22},detailHint:{fontSize:12,color:'#718277',marginTop:6},empty:{padding:20,color:'#5e7166',backgroundColor:'#fff',borderRadius:12,lineHeight:22},row:{flexDirection:'row',gap:9,alignItems:'center'},filterGrid:{gap:4},actionLine:{flexDirection:'row',alignItems:'center',justifyContent:'space-between',gap:8},smallButton:{borderRadius:8,paddingVertical:7,paddingHorizontal:14,backgroundColor:'#e3f3e9'},smallButtonText:{fontWeight:'800',color:'#145b3a'},field:{gap:6,marginTop:3},label:{fontSize:13,fontWeight:'800',color:'#284b3a',marginTop:5},input:{backgroundColor:'#fff',borderWidth:1,borderColor:'#ccdacf',borderRadius:10,minHeight:46,paddingHorizontal:12,fontSize:15,color:'#203b2e'},multiline:{minHeight:82,paddingTop:10},preview:{width:'100%',height:190,resizeMode:'contain',backgroundColor:'#eef2ef',borderRadius:9},cameraBox:{height:280,borderRadius:14,overflow:'hidden',backgroundColor:'#172b22',gap:6},camera:{flex:1},permission:{flex:1,alignItems:'center',justifyContent:'center',padding:16,gap:10},error:{padding:12,borderRadius:10,color:'#8f2f22',backgroundColor:'#fff0ed',lineHeight:20},notice:{padding:12,borderRadius:10,color:'#155c3d',backgroundColor:'#e4f4ea',lineHeight:20},warning:{padding:12,borderRadius:9,color:'#795500',backgroundColor:'#fff3cf',lineHeight:20},attribution:{fontSize:11,color:'#617469',lineHeight:16},sourceText:{padding:10,backgroundColor:'#fff',color:'#42554a',lineHeight:20},dangerText:{color:'#a72d22',fontWeight:'800'}});
