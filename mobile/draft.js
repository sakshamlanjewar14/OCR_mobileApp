const labels = {
  company_name:['company','company name','full company name'], owner_name:['owner','owner name','contact person'], designation:['designation','job title'],
  company_address:['address','company address','office address'], website:['website','web site'], facebook_link:['facebook','facebook link'],
  instagram_link:['instagram','instagram link'], whatsapp_link:['whatsapp link'], contact_numbers:['contact numbers','contact number','phone','phone number','mobile'],
  email_addresses:['email addresses','email address','email','email id'],
};
const cleanLabel = text => text.toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();

function replaceLabeledLine(text,key,value) {
  const aliases=(labels[key]||[]).map(cleanLabel); const lines=text.split('\n'); let found=false;
  const next=lines.map(line=>{const match=line.match(/^([^:]+):\s*(.*)$/);if(match&&aliases.includes(cleanLabel(match[1]))){found=true;return `${match[1]}: ${value}`;}return line;});
  return found?next.join('\n'):text;
}

function fieldsFromLabeledText(text,current) {
  const next={...current}; const entries=new Map(Object.entries(labels).flatMap(([key,aliases])=>aliases.map(alias=>[cleanLabel(alias),key])));
  for(const line of text.split('\n')){const match=line.match(/^([^:]+):\s*(.*)$/);if(!match)continue;const key=entries.get(cleanLabel(match[1]));if(!key)continue;const value=match[2].trim();
    if(key==='contact_numbers')next.contact_numbers=value.split(/[;,]+/).map(x=>x.trim()).filter(Boolean);
    else if(key==='email_addresses')next.email_addresses=value.split(/[;,]+/).map(x=>x.trim()).filter(Boolean);
    else next[key]=value;
  } return next;
}

module.exports={ replaceLabeledLine, fieldsFromLabeledText };
