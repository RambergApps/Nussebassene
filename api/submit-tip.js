const GITHUB_API = 'https://api.github.com';
const VALID_ROUNDS = new Set(['r32', 'r16', 'qf', 'sf', 'final']);

function send(res, statusCode, payload, extraHeaders = {}) {
  res.statusCode = statusCode;
  for (const [key, value] of Object.entries({
    'Content-Type': 'application/json; charset=utf-8',
    ...extraHeaders
  })) {
    res.setHeader(key, value);
  }
  res.end(JSON.stringify(payload));
}

function corsHeaders(req) {
  const configured = process.env.ALLOWED_ORIGIN || '*';
  const origin = req.headers.origin || '*';
  const allowOrigin = configured === '*' ? origin : configured;
  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, x-submit-token',
    'Vary': 'Origin'
  };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.on('data', chunk => {
      raw += chunk;
      if (raw.length > 1024 * 1024) {
        reject(new Error('Payload er for stor.'));
        req.destroy();
      }
    });
    req.on('end', () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        reject(new Error('Ugyldig JSON.'));
      }
    });
    req.on('error', reject);
  });
}

function requiredEnv(name) {
  const value = process.env[name];
  if (!value) throw new Error(`Mangler Vercel env: ${name}`);
  return value;
}

function normalizeFilename(name) {
  return String(name || '')
    .trim()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/æ/g, 'ae').replace(/ø/g, 'o').replace(/å/g, 'a')
    .replace(/Æ/g, 'Ae').replace(/Ø/g, 'O').replace(/Å/g, 'A')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'ukjent';
}

function isIntegerScore(value) {
  return Number.isInteger(value) && value >= 0 && value <= 30;
}

async function githubRequest(path, options = {}) {
  const token = requiredEnv('GITHUB_TOKEN');
  const response = await fetch(`${GITHUB_API}${path}`, {
    ...options,
    headers: {
      'Authorization': `Bearer ${token}`,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'nussebassene-submit',
      ...(options.headers || {})
    }
  });

  const text = await response.text();
  let body = {};
  try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }

  if (!response.ok) {
    const message = body.message || `GitHub API feilet med status ${response.status}`;
    const err = new Error(message);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

async function getFileSha(owner, repo, path, branch) {
  try {
    const encodedPath = path.split('/').map(encodeURIComponent).join('/');
    const data = await githubRequest(`/repos/${owner}/${repo}/contents/${encodedPath}?ref=${encodeURIComponent(branch)}`);
    return data.sha || null;
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

async function getJsonFile(owner, repo, path, branch) {
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  const data = await githubRequest(`/repos/${owner}/${repo}/contents/${encodedPath}?ref=${encodeURIComponent(branch)}`);
  if (!data.content) return null;
  const jsonText = Buffer.from(data.content, 'base64').toString('utf8');
  return JSON.parse(jsonText);
}

async function putJsonFile(owner, repo, path, branch, payload, message) {
  const sha = await getFileSha(owner, repo, path, branch);
  const content = Buffer.from(JSON.stringify(payload, null, 2) + '\n', 'utf8').toString('base64');
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  return githubRequest(`/repos/${owner}/${repo}/contents/${encodedPath}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, content, branch, ...(sha ? { sha } : {}) })
  });
}

async function triggerWorkflow(owner, repo, branch) {
  if (process.env.TRIGGER_WORKFLOW === 'false') return { skipped: true };
  try {
    return await githubRequest(`/repos/${owner}/${repo}/actions/workflows/oppdater.yml/dispatches`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: branch })
    });
  } catch (err) {
    // Ikke stopp innsending hvis token ikke har actions:write.
    return { skipped: false, error: err.message };
  }
}

function validateRequest(body) {
  const participant = String(body.participant || '').trim();
  const round = String(body.round || '').trim();
  const tips = Array.isArray(body.tips) ? body.tips : [];

  if (participant.length < 2 || participant.length > 80) throw new Error('Deltakernavn må være mellom 2 og 80 tegn.');
  if (!VALID_ROUNDS.has(round)) throw new Error('Ugyldig runde.');
  if (!tips.length) throw new Error('Ingen tips sendt inn.');
  if (tips.length > 20) throw new Error('For mange tips i én innsending.');

  const cleanedTips = tips.map(t => {
    const matchId = String(t.match_id || '').trim();
    const homeScore = Number(t.home_score);
    const awayScore = Number(t.away_score);
    if (!/^M\d{1,3}$/.test(matchId)) throw new Error(`Ugyldig kamp-ID: ${matchId}`);
    if (!isIntegerScore(homeScore) || !isIntegerScore(awayScore)) throw new Error(`Ugyldig tips for ${matchId}.`);
    return {
      match_id: matchId,
      match_no: Number(t.match_no || matchId.replace('M', '')),
      fifa_event_id: t.fifa_event_id ? String(t.fifa_event_id) : null,
      hjemme_snapshot: String(t.hjemme_snapshot || ''),
      borte_snapshot: String(t.borte_snapshot || ''),
      home_score: homeScore,
      away_score: awayScore
    };
  });

  return { participant, round, tips: cleanedTips };
}

function validateAgainstStatus(round, tips, status) {
  const matches = Array.isArray(status?.matches) ? status.matches : [];
  const byId = new Map(matches.map(m => [m.id, m]));
  const now = Date.now();

  for (const tip of tips) {
    const match = byId.get(tip.match_id);
    if (!match) throw new Error(`${tip.match_id} finnes ikke i status.json.`);
    if (match.runde !== round) throw new Error(`${tip.match_id} tilhører ikke runden ${round}.`);
    if (!match.utcDate) throw new Error(`${tip.match_id} mangler avsparkstid.`);
    if (!match.hjemme || !match.borte || match.hjemme === 'TBD' || match.borte === 'TBD') throw new Error(`${tip.match_id} mangler lag.`);
    if (new Date(match.utcDate).getTime() <= now) throw new Error(`${tip.match_id} har startet og er låst.`);
    if (match.tippebar === false || match.tippe_status !== 'åpen') throw new Error(`${tip.match_id} er ikke åpen for tipping.`);
  }
}

module.exports = async function handler(req, res) {
  const cors = corsHeaders(req);

  if (req.method === 'OPTIONS') {
    res.statusCode = 204;
    for (const [key, value] of Object.entries(cors)) res.setHeader(key, value);
    res.end();
    return;
  }

  if (req.method !== 'POST') {
    send(res, 405, { error: 'Kun POST er støttet.' }, cors);
    return;
  }

  try {
    const submitToken = process.env.SUBMIT_TOKEN;
    if (submitToken && req.headers['x-submit-token'] !== submitToken) {
      send(res, 401, { error: 'Ugyldig submit-token.' }, cors);
      return;
    }

    const owner = requiredEnv('GITHUB_OWNER');
    const repo = requiredEnv('GITHUB_REPO');
    const branch = process.env.GITHUB_BRANCH || 'main';

    const body = await readBody(req);
    const cleaned = validateRequest(body);
    const status = await getJsonFile(owner, repo, 'data/status.json', branch);
    validateAgainstStatus(cleaned.round, cleaned.tips, status);

    const payload = {
      deltaker: cleaned.participant,
      runde: cleaned.round,
      submitted_at: new Date().toISOString(),
      tips: cleaned.tips
    };

    const fileName = normalizeFilename(cleaned.participant);
    const path = `tippinger/${cleaned.round}/${fileName}.json`;
    await putJsonFile(owner, repo, path, branch, payload, `Tips: ${cleaned.participant} ${cleaned.round}`);
    const workflow = await triggerWorkflow(owner, repo, branch);

    send(res, 200, { ok: true, path, workflow }, cors);
  } catch (err) {
    const status = err.status && err.status >= 400 && err.status < 500 ? err.status : 500;
    send(res, status, { error: err.message || 'Ukjent feil.' }, cors);
  }
};
