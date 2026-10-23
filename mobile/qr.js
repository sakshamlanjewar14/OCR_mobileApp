const safeHttpUrl = value => {
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password && parsed.hostname ? value : null;
  } catch { return null; }
};

/** Classifies known QR payload formats on-device. It never requests or opens a decoded URL. */
function parseQrPayload(input) {
  const sourceText = String(input || '').trim();
  const result = { source_text: sourceText, contacts: [], emails: [], urls: [], unmapped: true };
  const fields = {};
  if (!sourceText) return result;
  const add = (key, value) => { if (value) fields[key] = value; };
  const addMany = (key, value) => { if (value) fields[key] = [...(fields[key] || []), value]; };
  const decode = value => decodeURIComponent(value).replace(/\\([,;\\])/g, '$1');

  if (/^BEGIN:VCARD/i.test(sourceText)) {
    const mapping = { FN:'owner_name', ORG:'company_name', TITLE:'designation', ADR:'company_address', TEL:'contact_numbers', EMAIL:'email_addresses', URL:'website' };
    for (const line of sourceText.replace(/\\n/g, '\n').split(/\r?\n/)) {
      const colon = line.indexOf(':'); if (colon < 0) continue;
      const key = line.slice(0, colon).split(';')[0].split('.').pop().toUpperCase(); const field = mapping[key];
      if (!field) continue;
      let value = decode(line.slice(colon + 1).trim());
      if (field === 'company_address') value = value.split(';').filter(Boolean).join(', ');
      if (['contact_numbers','email_addresses'].includes(field)) addMany(field,value); else add(field,value);
    }
  } else if (/^MECARD:/i.test(sourceText)) {
    const mapping = { N:'owner_name', ORG:'company_name', TEL:'contact_numbers', EMAIL:'email_addresses', ADR:'company_address', URL:'website' };
    for (const match of sourceText.slice(7).matchAll(/(?:^|;)\s*([A-Z]+):((?:\\.|[^;])*)/gi)) {
      const field = mapping[match[1].toUpperCase()]; if (!field) continue;
      let value = decode(match[2]); if(field==='company_address') value=value.split(',').filter(Boolean).join(', ');
      if(['contact_numbers','email_addresses'].includes(field)) addMany(field,value); else add(field,value);
    }
  } else if (/^tel:/i.test(sourceText)) addMany('contact_numbers',decode(sourceText.slice(4).split('?')[0]));
  else if (/^mailto:/i.test(sourceText)) addMany('email_addresses',decode(sourceText.slice(7).split('?')[0]));
  else if (/^geo:/i.test(sourceText)) {
    const match = sourceText.match(/^geo:([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)/i);
    if (match) fields.location_coordinates={latitude:Number(match[1]),longitude:Number(match[2])};
  } else {
    const candidate = /^https?:\/\//i.test(sourceText) ? safeHttpUrl(sourceText) : null;
    if(candidate) {
      const url = new URL(candidate); const host=url.hostname.toLowerCase().replace(/^www\./,''); result.urls=[candidate];
      if(['facebook.com','m.facebook.com','fb.com'].includes(host)) add('facebook_link',candidate);
      else if(host==='instagram.com') add('instagram_link',candidate);
      else if(['wa.me','api.whatsapp.com','whatsapp.com','chat.whatsapp.com'].includes(host)) add('whatsapp_link',candidate);
      else if(['maps.google.com','google.com','maps.app.goo.gl','goo.gl','openstreetmap.org'].includes(host)) {
        const match=(url.pathname+url.search).match(/@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)/);
        const query=url.searchParams.get('q')||''; const qmatch=query.match(/^\s*([+-]?\d+(?:\.\d+)?),\s*([+-]?\d+(?:\.\d+)?)\s*$/);
        if(match||qmatch) { const m=match||qmatch; fields.location_coordinates={latitude:Number(m[1]),longitude:Number(m[2])}; }
        else fields.map_url=candidate;
      } else if(host==='singleqr.in'||host.endsWith('.singleqr.in')||['bit.ly','tinyurl.com','t.co','qrco.de'].includes(host)) fields.qr_redirect_url=candidate;
      else add('website',candidate);
    } else if (!/^[a-z][a-z0-9+.-]*:/i.test(sourceText) && /@/.test(sourceText) && /\.[A-Za-z]{2,}/.test(sourceText)) addMany('email_addresses',sourceText);
  }
  for (const [key,value] of Object.entries(fields)) {
    if(key==='contact_numbers') result.contacts=value;
    else if(key==='email_addresses') result.emails=value;
    else result[key]=value;
  }
  result.unmapped=Object.keys(fields).length===0;
  return result;
}

module.exports = { parseQrPayload };
