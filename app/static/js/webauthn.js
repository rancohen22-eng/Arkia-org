// Minimal WebAuthn browser helpers for Arkia passkey login (no external libs).
(function(global){
  function b64urlToBuf(s){
    s = s.replace(/-/g,'+').replace(/_/g,'/');
    const pad = s.length % 4 ? '='.repeat(4 - s.length % 4) : '';
    const bin = atob(s + pad);
    const buf = new Uint8Array(bin.length);
    for (let i=0;i<bin.length;i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }
  function bufToB64url(buf){
    const bytes = new Uint8Array(buf); let bin = '';
    for (const b of bytes) bin += String.fromCharCode(b);
    return btoa(bin).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  }
  async function postJSON(url, data){
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(data||{})});
    const j = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error || ('שגיאה ' + r.status));
    return j;
  }

  async function register(label){
    if(!global.PublicKeyCredential) throw new Error('הדפדפן אינו תומך ב-Passkey');
    const opts = await postJSON('/auth/webauthn/register/options', {});
    opts.challenge = b64urlToBuf(opts.challenge);
    opts.user.id = b64urlToBuf(opts.user.id);
    (opts.excludeCredentials||[]).forEach(c => c.id = b64urlToBuf(c.id));
    const cred = await navigator.credentials.create({publicKey: opts});
    const payload = {
      id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: bufToB64url(cred.response.clientDataJSON),
        attestationObject: bufToB64url(cred.response.attestationObject),
        transports: cred.response.getTransports ? cred.response.getTransports() : undefined
      },
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
      clientExtensionResults: cred.getClientExtensionResults()
    };
    return postJSON('/auth/webauthn/register/verify', {credential: payload, label: label||''});
  }

  async function login(username){
    if(!global.PublicKeyCredential) throw new Error('הדפדפן אינו תומך ב-Passkey');
    const opts = await postJSON('/auth/webauthn/login/options', {username});
    opts.challenge = b64urlToBuf(opts.challenge);
    (opts.allowCredentials||[]).forEach(c => c.id = b64urlToBuf(c.id));
    const cred = await navigator.credentials.get({publicKey: opts});
    const payload = {
      id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: bufToB64url(cred.response.clientDataJSON),
        authenticatorData: bufToB64url(cred.response.authenticatorData),
        signature: bufToB64url(cred.response.signature),
        userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null
      },
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
      clientExtensionResults: cred.getClientExtensionResults()
    };
    return postJSON('/auth/webauthn/login/verify', {credential: payload});
  }

  global.ArkiaPasskey = {register, login};
})(window);
