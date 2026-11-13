/**
 * Test de non-régression — REGISTRE DES LITIGES & RELANCES (index.html).
 *
 * Il lit index.html EN DIRECT : il extrait le bloc du registre et le générateur
 * de courrier du fichier réel, puis les éprouve. Un renommage, un changement de
 * format de clé ou une régression dans les niveaux de relance font donc échouer
 * ce test, ce qu'une copie figée du code ne verrait pas.
 *
 * Le moteur lui-même (_verifRenderLitige) n'est PAS testé ici : il est en
 * production et vérifié sur données réelles. On éprouve le CONTRAT entre lui et
 * le registre — stabilité de la clé, échéances, transitions, courriers.
 *
 * Lancer : node test_litige_registry.js
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const SRC = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');

// ── Extraction : accolades équilibrées, en sautant commentaires, chaînes,
//    gabarits et littéraux d'expression rationnelle. Les apostrophes des
//    commentaires français piègent tout scanner naïf.
function scanBlock(s, i) {
  let d = 0, j = i, prev = '';
  const n = s.length;
  while (j < n) {
    const c = s[j], two = s.slice(j, j + 2);
    if (two === '//') { j = s.indexOf('\n', j); if (j < 0) return -1; continue; }
    if (two === '/*') { j = s.indexOf('*/', j); if (j < 0) return -1; j += 2; continue; }
    if (c === '"' || c === "'" || c === '`') {
      const q = c; j++;
      while (j < n) {
        if (s[j] === '\\') { j += 2; continue; }
        if (s[j] === q) break;
        if (q === '`' && s.slice(j, j + 2) === '${') {
          const k = scanBlock(s, s.indexOf('{', j));
          if (k < 0) return -1;
          j = k + 1; continue;
        }
        j++;
      }
      j++; continue;
    }
    if (c === '/' && '(,=:[!&|?{};+-*%~^<>\n'.includes(prev)) {
      j++;
      while (j < n && s[j] !== '/') {
        if (s[j] === '\\') j++;
        else if (s[j] === '[') { while (j < n && s[j] !== ']') { if (s[j] === '\\') j++; j++; } }
        j++;
      }
      j++; continue;
    }
    if (c === '{') d++;
    else if (c === '}') { d--; if (d === 0) return j; }
    if (!/\s/.test(c)) prev = c;
    j++;
  }
  return -1;
}
function grabFn(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(', 'g');
  let m;
  while ((m = re.exec(SRC))) {
    let p = m.index + m[0].length - 1, d = 0;
    while (p < SRC.length) {               // sauter la liste de paramètres
      if (SRC[p] === '(') d++;
      else if (SRC[p] === ')') { d--; if (!d) break; }
      p++;
    }
    const i = SRC.indexOf('{', p), j = scanBlock(SRC, i);
    if (j > 0) return SRC.slice(m.index, j + 1);
  }
  throw new Error('introuvable dans index.html : ' + name);
}
function grabRegistryBlock() {
  const start = SRC.indexOf('// REGISTRE DES LITIGES & RELANCES');
  const end   = SRC.indexOf('// ── Détail virement par virement', start);
  if (start < 0 || end < 0) throw new Error('bloc du registre introuvable dans index.html');
  return SRC.slice(SRC.lastIndexOf('// ═', start), end);
}

// Faux environnement : état, labos sous contrat, moteur de litiges simulé.
// Encodé en base64 : le code contient des gabarits et des antislashs que
// n'importe quel échappement de chaîne finirait par trahir.
const STUBS = Buffer.from(
  'bGV0IG9rID0gMCwga28gPSAwOwpjb25zdCBjaGVjayA9IChsYWJlbCwgZ290LCB3YW50KSA9PiB7CiAgY29uc3QgZ29vZCA9' +
  'IEpTT04uc3RyaW5naWZ5KGdvdCkgPT09IEpTT04uc3RyaW5naWZ5KHdhbnQpOwogIGNvbnNvbGUubG9nKChnb29kID8gJyAg' +
  '4pyUICcgOiAnICDinJggJykgKyBsYWJlbCArICcg4oaSICcgKyBKU09OLnN0cmluZ2lmeShnb3QpICsKICAgICAgICAgICAg' +
  'ICAoZ29vZCA/ICcnIDogJyAgIEFUVEVORFUgJyArIEpTT04uc3RyaW5naWZ5KHdhbnQpKSk7CiAgZ29vZCA/IG9rKysgOiBr' +
  'bysrOwp9Owpjb25zdCBsaWtlID0gKGxhYmVsLCB0eHQsIHJlKSA9PiB7CiAgY29uc3QgZ29vZCA9IHJlLnRlc3QodHh0KTsK' +
  'ICBjb25zb2xlLmxvZygoZ29vZCA/ICcgIOKclCAnIDogJyAg4pyYICcpICsgbGFiZWwgKyAoZ29vZCA/ICcnIDogJyAgIE1B' +
  'TlFVRSAnICsgcmUpKTsKICBnb29kID8gb2srKyA6IGtvKys7Cn07CgovLyDilIDilIAgRmF1eCBlbnZpcm9ubmVtZW50IOKU' +
  'gOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKU' +
  'gOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKU' +
  'gOKUgOKUgOKUgOKUgOKUgApsZXQgc2F2ZWQgPSAwOwpnbG9iYWwuaXNBZG1pbiA9IHRydWU7Cmdsb2JhbC5zaG93VG9hc3Qg' +
  'PSAoKSA9PiB7fTsKZ2xvYmFsLnNhdmVTdGF0ZSA9ICgpID0+IHsgc2F2ZWQrKzsgfTsKZ2xvYmFsLnN0YXRlID0gewogIF9s' +
  'aXRpZ2VSZWdpc3RyeToge30sCiAgbGFiUGFnZXM6IFsKICAgIHsgc2VsZWN0ZWRMYWI6ICdCaW9nYXJhbicsIGNvbnRyYWN0' +
  'U2lnbmVkOiB0cnVlLCAgY29udHJhY3RTaWduZWQyNTogdHJ1ZSB9LAogICAgeyBzZWxlY3RlZExhYjogJ1p5ZHVzJywgICAg' +
  'Y29udHJhY3RTaWduZWQ6IHRydWUsICBjb250cmFjdFNpZ25lZDI1OiBmYWxzZSB9LAogICAgeyBzZWxlY3RlZExhYjogJ1Rl' +
  'dmEnLCAgICAgY29udHJhY3RTaWduZWQ6IGZhbHNlLCBjb250cmFjdFNpZ25lZDI1OiBmYWxzZSB9LAogIF0sCn07Cmdsb2Jh' +
  'bC5kb2N1bWVudCA9IHsgZ2V0RWxlbWVudEJ5SWQ6ICgpID0+IG51bGwgfTsgICAvLyBwYXMgZGUgRE9NIDogb24gdGVzdGUg' +
  'bGEgbG9naXF1ZQpnbG9iYWwuX3ZlcmlmTm9ybUxhYiA9IHMgPT4gU3RyaW5nKHMgfHwgJycpLnRvVXBwZXJDYXNlKCk7Cmds' +
  'b2JhbC53aW5kb3cgPSBnbG9iYWw7ICAgLy8gd2luZG93LnggPT09IHgsIGNvbW1lIGRhbnMgdW4gbmF2aWdhdGV1cgoKLy8g' +
  'TGUgbW90ZXVyIGVzdCBzaW11bMOpIDogb24gbHVpIGZhaXQgcHJvZHVpcmUgZXhhY3RlbWVudCBsYSBmb3JtZSBkZSBsaWdu' +
  'ZXMKLy8gcXUnaWwgcHJvZHVpdCBlbiB2cmFpIChuYXR1cmVzIHVucGFpZCAvIHVuZGVyYmlsbGVkIC8gbm9hdm9pciArIGNv' +
  'b3Agb2ZmaWNpZXVzZSkuCmNvbnN0IFlSID0gbmV3IERhdGUoKS5nZXRGdWxsWWVhcigpOwpnbG9iYWwuX3ZlcmlmUmVuZGVy' +
  'TGl0aWdlID0gKHlyLCBub3JtLCBsYWIpID0+IHsKICBjb25zdCByb3dzID0gbm9ybSA9PT0gJ0JJT0dBUkFOJwogICAgPyBb' +
  'eyBtazogYCR7eXJ9LTAzYCwgbTogMywgbmF0dXJlOiAndW5wYWlkJywgICAgICByZWZzOiAnOTAwNjE4NzcwMicsIGFubm9u' +
  'Y2U6IDEzOTYuMjksIHZlcnNlOiAwLCAgZHVlOiAxMzk2LjI5LCBwcmV1dmU6ICdwMScgfSwKICAgICAgIHsgbWs6IGAke3ly' +
  'fS0wOWAsIG06IDksIG5hdHVyZTogJ3VuZGVyYmlsbGVkJywgcmVmczogJzkwMDYyNDk4ODYnLCBhbm5vbmNlOiA4MDAsICBh' +
  'dHRlbmR1OiAxNTAwLCBkdWU6IDcwMCwgcHJldXZlOiAncDInIH0sCiAgICAgICB7IG1rOiBgZGVwdWlzIDAxLzA2LzIwMjVg' +
  'LCBuYXR1cmU6ICdjb29wJywgb2ZmaWNpZXV4OiB0cnVlLCBkdWU6IDUwMDAsIHByZXV2ZTogJ2Nvb3AnIH1dCiAgICA6IFt7' +
  'IG1rOiBgJHt5cn0tMDVgLCBtOiA1LCBuYXR1cmU6ICdub2F2b2lyJywgYXR0ZW5kdTogMzAwLCB2ZXJzZTogMCwgZHVlOiAz' +
  'MDAsIHByZXV2ZTogJ3AzJyB9XTsKICB3aW5kb3cuX2xpdGlnZURhdGEgPSB3aW5kb3cuX2xpdGlnZURhdGEgfHwge307CiAg' +
  'd2luZG93Ll9saXRpZ2VEYXRhW2Ake25vcm19fCR7eXJ9YF0gPSB7IHJvd3MsIHRvdGFsOiByb3dzLmZpbHRlcihyID0+ICFy' +
  'Lm9mZmljaWV1eCkucmVkdWNlKChhLCByKSA9PiBhICsgci5kdWUsIDApLAogICAgICAgICAgICAgICAgICAgICAgICAgICAg' +
  'ICAgICAgICAgICAgICBsYWJEaXNwbGF5OiBsYWIsIHlyLCBjc0ZyOiAnMDEvMDYvMjAyNScgfTsKfTsKCg==', 'base64').toString('utf8');

// Assertions.
// Encodé en base64 : le code contient des gabarits et des antislashs que
// n'importe quel échappement de chaîne finirait par trahir.
const ASSERTS = Buffer.from(
  'CgovLyDilIDilIAgMS4gQ2zDqSBuYXR1cmVsbGUg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACmNvbnNvbGUubG9nKCfilIDi' +
  'lIAgY2zDqSBuYXR1cmVsbGUgOiBzdGFibGUsIHNhbnMgaWRlbnRpZmlhbnQgZ8OpbsOpcsOpIOKUgOKUgCcpOwpjb25zdCBy' +
  'ID0geyBtazogJzIwMjYtMDMnLCBuYXR1cmU6ICd1bnBhaWQnIH07CmNoZWNrKCdjbMOpIHJlcHJvZHVjdGlibGUnLCBfbGl0' +
  'aWdlS2V5KCdCSU9HQVJBTicsIDIwMjYsIHIpLCAnQklPR0FSQU58MjAyNnwyMDI2LTAzfHVucGFpZCcpOwpjaGVjaygnZGV1' +
  'eCBhcHBlbHMgaWRlbnRpcXVlcycsIF9saXRpZ2VLZXkoJ0JJT0dBUkFOJywgMjAyNiwgcikgPT09IF9saXRpZ2VLZXkoJ0JJ' +
  'T0dBUkFOJywgMjAyNiwgeyAuLi5yIH0pLCB0cnVlKTsKCi8vIOKUgOKUgCAyLiDDiWNow6lhbmNlcyDilIDilIDilIDilIDi' +
  'lIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDi' +
  'lIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDi' +
  'lIDilIDilIDilIDilIDilIDilIDilIAKY29uc29sZS5sb2coJ1xu4pSA4pSAIMOpY2jDqWFuY2VzIDogSis3IGFwcsOocyBy' +
  'w6ljbGFtYXRpb24sIEorMTUsIEorMzAgYXUgdG90YWwg4pSA4pSAJyk7CmNoZWNrKCfDoCByw6ljbGFtZXIgOiBhdWN1bmUg' +
  'w6ljaMOpYW5jZScsIF9saXRpZ2VEdWUoeyBzdGF0dXQ6ICdhX3JlY2xhbWVyJywgZGVwdWlzOiBudWxsIH0pLCBudWxsKTsK' +
  'Y2hlY2soJ3LDqWNsYW3DqSBsZSAwMS8wMyDihpIgSis3JywgICBfbGl0aWdlRHVlKHsgc3RhdHV0OiAncmVjbGFtZScsICBk' +
  'ZXB1aXM6ICcyMDI2LTAzLTAxJyB9KSwgJzIwMjYtMDMtMDgnKTsKY2hlY2soJzFyZSByZWxhbmNlIGxlIDA4LzAzIOKGkiBK' +
  'KzE1IGF1IHRvdGFsJywgX2xpdGlnZUR1ZSh7IHN0YXR1dDogJ3JlbGFuY2UxJywgZGVwdWlzOiAnMjAyNi0wMy0wOCcgfSks' +
  'ICcyMDI2LTAzLTE2Jyk7CmNoZWNrKCcyZSByZWxhbmNlIGxlIDE2LzAzIOKGkiBKKzMxIGF1IHRvdGFsJywgIF9saXRpZ2VE' +
  'dWUoeyBzdGF0dXQ6ICdyZWxhbmNlMicsIGRlcHVpczogJzIwMjYtMDMtMTYnIH0pLCAnMjAyNi0wMy0zMScpOwpjaGVjaygn' +
  'csOpZ2zDqSA6IHBsdXMgZOKAmcOpY2jDqWFuY2UnLCBfbGl0aWdlRHVlKHsgc3RhdHV0OiAncmVnbGUnLCBkZXB1aXM6ICcy' +
  'MDI2LTAzLTMxJyB9KSwgbnVsbCk7CmNoZWNrKCdhYmFuZG9ubsOpIDogcGx1cyBk4oCZw6ljaMOpYW5jZScsIF9saXRpZ2VE' +
  'dWUoeyBzdGF0dXQ6ICdhYmFuZG9ubmUnLCBkZXB1aXM6ICcyMDI2LTAzLTMxJyB9KSwgbnVsbCk7CgovLyDilIDilIAgMy4g' +
  'Q29sbGVjdGUg4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSACmNvbnNvbGUubG9nKCdcbuKUgOKUgCBj' +
  'b2xsZWN0ZSBkZXMgcsOpY2xhbWF0aW9ucyDilIDilIAnKTsKY29uc3QgY2xhaW1zID0gX2xpdGlnZUFsbENsYWltcygpOwpj' +
  'aGVjaygnbGFib3Mgc291cyBjb250cmF0IHVuaXF1ZW1lbnQgKFRldmEgZXhjbHUpJywKICAgICAgWy4uLm5ldyBTZXQoY2xh' +
  'aW1zLm1hcChjID0+IGMubGFiKSldLnNvcnQoKSwgWydCaW9nYXJhbicsICdaeWR1cyddKTsKY2hlY2soJ2Nvb3AgZXhjbHVl' +
  'IGR1IHJlZ2lzdHJlJywgY2xhaW1zLnNvbWUoYyA9PiBjLnJvdy5uYXR1cmUgPT09ICdjb29wJyksIGZhbHNlKTsKY2hlY2so' +
  'J3N0YXR1dCBwYXIgZMOpZmF1dCcsIFsuLi5uZXcgU2V0KGNsYWltcy5tYXAoYyA9PiBjLmVudHJ5LnN0YXR1dCkpXSwgWydh' +
  'X3JlY2xhbWVyJ10pOwpjb25zdCBiaW8gPSBjbGFpbXMuZmlsdGVyKGMgPT4gYy5sYWIgPT09ICdCaW9nYXJhbicgJiYgYy55' +
  'ciA9PT0gWVIpOwpjaGVjaygnQmlvZ2FyYW4gYW5uw6llIGNvdXJhbnRlIDogMiByw6ljbGFtYXRpb25zJywgYmlvLmxlbmd0' +
  'aCwgMik7CmNoZWNrKCdtb250YW50cyByZXByaXMgZHUgbW90ZXVyJywgYmlvLm1hcChjID0+IGMubW9udGFudCkuc29ydCgo' +
  'YSwgYikgPT4gYiAtIGEpLCBbMTM5Ni4yOSwgNzAwXSk7CgovLyDilIDilIAgNC4gVHJhbnNpdGlvbnMg4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA4pSA' +
  '4pSA4pSA4pSA4pSA4pSA4pSACmNvbnNvbGUubG9nKCdcbuKUgOKUgCB0cmFuc2l0aW9ucyBkZSBzdGF0dXQg4pSA4pSAJyk7' +
  'CmNvbnN0IGsgPSBfbGl0aWdlS2V5KCdCSU9HQVJBTicsIFlSLCB7IG1rOiBgJHtZUn0tMDNgLCBuYXR1cmU6ICd1bnBhaWQn' +
  'IH0pOwpfbGl0aWdlU2V0U3RhdHV0KGssICdyZWNsYW1lJyk7CmNvbnN0IGUxID0gX2xpdGlnZUVudHJ5KGspOwpjaGVjaygn' +
  'c3RhdHV0IGVucmVnaXN0csOpJywgZTEuc3RhdHV0LCAncmVjbGFtZScpOwpjaGVjaygnZGF0ZSBk4oCZZW50csOpZSBwb3PD' +
  'qWUnLCBlMS5kZXB1aXMsIF9saXRpZ2VUb2RheSgpKTsKY2hlY2soJ21vbnRhbnQgZGUgcsOpZsOpcmVuY2UgZmlnw6knLCBl' +
  'MS5tb250YW50MCwgMTM5Ni4yOSk7CmNoZWNrKCdoaXN0b3JpcXVlIDogMSB0cmFuc2l0aW9uJywgKGUxLmhpc3RvcmlxdWUg' +
  'fHwgW10pLmxlbmd0aCwgMSk7CmNoZWNrKCdoaXN0b3JpcXVlIDogZGUg4oaSIHZlcnMnLCBbZTEuaGlzdG9yaXF1ZVswXS5k' +
  'ZSwgZTEuaGlzdG9yaXF1ZVswXS52ZXJzXSwgWydhX3JlY2xhbWVyJywgJ3JlY2xhbWUnXSk7CmNoZWNrKCdzYXV2ZWdhcmRl' +
  'IGTDqWNsZW5jaMOpZScsIHNhdmVkID4gMCwgdHJ1ZSk7Cl9saXRpZ2VTZXRTdGF0dXQoaywgJ3JlbGFuY2UxJyk7CmNoZWNr' +
  'KCdoaXN0b3JpcXVlIGN1bXVsw6knLCBfbGl0aWdlRW50cnkoaykuaGlzdG9yaXF1ZS5sZW5ndGgsIDIpOwpjaGVjaygnbW9u' +
  'dGFudCBkZSByw6lmw6lyZW5jZSBpbmNoYW5nw6knLCBfbGl0aWdlRW50cnkoaykubW9udGFudDAsIDEzOTYuMjkpOwpfbGl0' +
  'aWdlU2V0U3RhdHV0KGssICdyZWdsZScpOwpjaGVjaygncsOpZ2zDqSBlc3QgdGVybWluYWwnLCAoX0xJVElHRV9GTE9XW19s' +
  'aXRpZ2VFbnRyeShrKS5zdGF0dXRdIHx8IHt9KS5maW5hbCwgdHJ1ZSk7CmNoZWNrKCdzdGF0dXQgaW5jb25udSByZWZ1c8Op' +
  'JywgKCgpID0+IHsgX2xpdGlnZVNldFN0YXR1dChrLCAnbl9pbXBvcnRlX3F1b2knKTsgcmV0dXJuIF9saXRpZ2VFbnRyeShr' +
  'KS5zdGF0dXQ7IH0pKCksICdyZWdsZScpOwoKY29uc29sZS5sb2coJ1xu4pSA4pSAIGxlY3R1cmUgc2V1bGUgOiBhdWN1biDD' +
  'qWNyaXQg4pSA4pSAJyk7Cmdsb2JhbC5pc0FkbWluID0gZmFsc2U7CmNvbnN0IGsyID0gX2xpdGlnZUtleSgnWllEVVMnLCBZ' +
  'UiwgeyBtazogYCR7WVJ9LTA1YCwgbmF0dXJlOiAnbm9hdm9pcicgfSk7Cl9saXRpZ2VTZXRTdGF0dXQoazIsICdyZWNsYW1l' +
  'Jyk7CmNoZWNrKCdub24tYWRtaW4gOiBzdGF0dXQgaW5jaGFuZ8OpJywgX2xpdGlnZUVudHJ5KGsyKS5zdGF0dXQsICdhX3Jl' +
  'Y2xhbWVyJyk7Cmdsb2JhbC5pc0FkbWluID0gdHJ1ZTsKCi8vIOKUgOKUgCA1LiBOaXZlYXV4IGRlIGNvdXJyaWVyIOKUgOKU' +
  'gOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKU' +
  'gOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKUgOKU' +
  'gApjb25zb2xlLmxvZygnXG7ilIDilIAgbGVzIHF1YXRyZSBuaXZlYXV4IGRlIGNvdXJyaWVyIOKUgOKUgCcpOwpfdmVyaWZS' +
  'ZW5kZXJMaXRpZ2UoWVIsICdCSU9HQVJBTicsICdCaW9nYXJhbicpOwpjb25zdCBrZXkgPSBgQklPR0FSQU58JHtZUn1gOwpj' +
  'b25zdCBMID0gbHZsID0+IF92ZXJpZkxpdGlnZUxldHRlcihrZXksIGx2bCk7Cmxpa2UoJ3LDqWNsYW1hdGlvbiA6IG9iamV0' +
  'JywgICAgICAgIEwoJ3JlY2xhbWF0aW9uJyksICAgICAvXk9iamV0IDogUsOpY2xhbWF0aW9uLyk7Cmxpa2UoJzFyZSByZWxh' +
  'bmNlIDogb2JqZXQnLCAgICAgICAgTCgncmVsYW5jZTEnKSwgICAgICAgIC9eT2JqZXQgOiAxcmUgcmVsYW5jZS8pOwpsaWtl' +
  'KCcxcmUgcmVsYW5jZSA6IHNhbnMgcsOpcG9uc2UnLCBMKCdyZWxhbmNlMScpLCAgICAgICAgL2RlbWV1csOpZSBzYW5zIHLD' +
  'qXBvbnNlLyk7Cmxpa2UoJzJlIHJlbGFuY2UgOiA4IGpvdXJzJywgICAgICAgTCgncmVsYW5jZTInKSwgICAgICAgIC9odWl0' +
  'IFwoOFwpIGpvdXJzLyk7Cmxpa2UoJzJlIHJlbGFuY2UgOiBhbm5vbmNlIGxhIExSQVInLCBMKCdyZWxhbmNlMicpLCAgICAg' +
  'IC9sZXR0cmUgcmVjb21tYW5kw6llIGF2ZWMgYWNjdXPDqSBkZSByw6ljZXB0aW9uL2kpOwpsaWtlKCdtaXNlIGVuIGRlbWV1' +
  'cmUgOiBvYmpldCcsICAgIEwoJ21pc2VfZW5fZGVtZXVyZScpLCAvXk9iamV0IDogTUlTRSBFTiBERU1FVVJFIGRlIHBheWVy' +
  'Lyk7Cmxpa2UoJ21pc2UgZW4gZGVtZXVyZSA6IG1lbnRpb24gTFJBUicsIEwoJ21pc2VfZW5fZGVtZXVyZScpLCAvTGV0dHJl' +
  'IHJlY29tbWFuZMOpZSBhdmVjIGFjY3Vzw6kgZGUgcsOpY2VwdGlvblwuLyk7Cmxpa2UoJ21pc2UgZW4gZGVtZXVyZSA6IGlu' +
  'dMOpcsOqdHMgZXQgaW5kZW1uaXTDqScsIEwoJ21pc2VfZW5fZGVtZXVyZScpLCAvaW50w6lyw6p0cyBkZSByZXRhcmQgZXQg' +
  'bCdpbmRlbW5pdMOpIGZvcmZhaXRhaXJlLyk7CmNoZWNrKCduaXZlYXUgaW5jb25udSDihpIgcsOpY2xhbWF0aW9uJywgTCgn' +
  'bmF3YWsnKS5zdGFydHNXaXRoKCdPYmpldCA6IFLDqWNsYW1hdGlvbicpLCB0cnVlKTsKY2hlY2soJ3NhbnMgbml2ZWF1IOKG' +
  'kiByw6ljbGFtYXRpb24gKGNvbXBhdGliaWxpdMOpKScsIF92ZXJpZkxpdGlnZUxldHRlcihrZXkpLnN0YXJ0c1dpdGgoJ09i' +
  'amV0IDogUsOpY2xhbWF0aW9uJyksIHRydWUpOwoKY29uc29sZS5sb2coJ1xu4pSA4pSAIGxhIHByZXV2ZSBuZSB2YXJpZSBw' +
  'YXMgZOKAmXVuIG5pdmVhdSDDoCBs4oCZYXV0cmUg4pSA4pSAJyk7CmNvbnN0IGNvcnBzID0gdCA9PiB0LnNwbGl0KCdcblxu' +
  'JykuZmlsdGVyKHAgPT4gL15cZFwuIC8udGVzdChwKSkuam9pbignXG4nKTsKY2hlY2soJ3BhcmFncmFwaGVzIG51bcOpcm90' +
  'w6lzIGlkZW50aXF1ZXMnLAogICAgICBbJ3JlbGFuY2UxJywgJ3JlbGFuY2UyJywgJ21pc2VfZW5fZGVtZXVyZSddLmV2ZXJ5' +
  'KGwgPT4gY29ycHMoTChsKSkgPT09IGNvcnBzKEwoJ3JlY2xhbWF0aW9uJykpKSwgdHJ1ZSk7CmNoZWNrKCdjb29wIGFic2Vu' +
  'dGUgZHUgY291cnJpZXInLCAvQ09PUHxwcmVzdGF0aW9ucyBlbiByZXRhcmQvLnRlc3QoTCgncmVjbGFtYXRpb24nKSksIGZh' +
  'bHNlKTsKCmNvbnNvbGUubG9nKCdcbicgKyAnPScucmVwZWF0KDU2KSArIGBcbiAgJHtva30gcsOpdXNzaXMsICR7a299IMOp' +
  'Y2hvdcOpc1xuYCArICc9Jy5yZXBlYXQoNTYpKTsKcHJvY2Vzcy5leGl0KGtvID8gMSA6IDApOwo=', 'base64').toString('utf8');

const CODE = grabRegistryBlock() + '\n' + grabFn('_verifLitigeLetter');

// ── Exécution : tout dans UN seul contexte, stubs puis code puis assertions.
//    Les déclarations `const` du bloc doivent vivre dans la même portée que les
//    assertions, ce qu'un eval séparé ne permet pas.
const sandbox = { console, JSON, Date, Math, Object, Array, String, Number, RegExp, process };
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(STUBS + '\n' + CODE + '\n' + ASSERTS, sandbox,
                { filename: 'registre-litiges' });
