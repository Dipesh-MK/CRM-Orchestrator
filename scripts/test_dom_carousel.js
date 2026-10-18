// scripts/test_dom_carousel.js
// Minimal Node DOM Harness for Template Carousel and Approval UI verification
const fs = require('fs');
const path = require('path');

const htmlPath = path.join(__dirname, '..', 'static', 'index.html');
const htmlSource = fs.readFileSync(htmlPath, 'utf8');

// Lightweight in-memory DOM Node implementation
class MockElement {
  constructor(id, tagName = 'div') {
    this.id = id;
    this.tagName = tagName;
    this.textContent = '';
    this._innerHTML = '';
    this.disabled = false;
    this._classes = new Set();
    this.attributes = {};
    this.onclick = null;
  }

  get classList() {
    return {
      add: (...cls) => cls.forEach(c => this._classes.add(c)),
      remove: (...cls) => cls.forEach(c => this._classes.delete(c)),
      contains: (c) => this._classes.has(c),
      toString: () => Array.from(this._classes).join(' ')
    };
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(val) {
    this._innerHTML = val;
    // Extract text content approximation
    this.textContent = val.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  insertAdjacentHTML(position, html) {
    this._innerHTML += html;
    this.textContent = this._innerHTML.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }

  click() {
    if (this.disabled) {
      throw new Error(`Cannot click disabled button #${this.id}`);
    }
    if (typeof this.onclick === 'function') {
      return this.onclick();
    }
  }
}

// Build DOM tree
const elements = {};
function getOrCreate(id, tag = 'div') {
  if (!elements[id]) elements[id] = new MockElement(id, tag);
  return elements[id];
}

// Pre-create all elements referenced in carousel and approval
[
  'templatesContainer', 'templatesList', 'scheduleText',
  'prevTemplateBtn', 'nextTemplateBtn', 'templateCounterBadge',
  'approveCampaignBtn', 'rejectCampaignBtn', 'exportCampaignBtn',
  'editTemplateModal', 'editModalText', 'editModalTitle',
  'repromptTemplateModal', 'repromptModalText', 'repromptModalTitle',
  'sendAggregatorContainer', 'chatMessages', 'healthText', 'healthIndicator'
].forEach(id => getOrCreate(id));

const document = {
  getElementById: (id) => elements[id] || null,
  activeElement: { tagName: 'BODY' }
};

const keyListeners = [];
const window = {
  addEventListener: (event, handler) => {
    if (event === 'keydown') keyListeners.push(handler);
  }
};

const fetchCalls = [];
global.fetch = async (url, opts = {}) => {
  fetchCalls.push({ url, opts });
  if (url.includes('/api/session/') && url.includes('/csrf')) {
    return {
      ok: true,
      status: 200,
      json: async () => ({ csrf_token: 'csrf_mock_token_128bits' })
    };
  }
  if (url === '/api/campaign/approve') {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'success',
        session_id: 'sess_dom_test',
        approved: true
      })
    };
  }
  return { ok: true, status: 200, json: async () => ({ status: 'success' }) };
};

global.document = document;
global.window = window;
global.alert = (msg) => console.log('[UI Alert]:', msg);

// Extract the script tag functions from static/index.html
const scriptStart = htmlSource.indexOf('<script>');
const scriptEnd = htmlSource.lastIndexOf('</script>');
const scriptContent = htmlSource.substring(scriptStart + 8, scriptEnd);

// Execute the UI script in our mock environment
const vm = require('vm');
const context = {
  document,
  window,
  fetch: global.fetch,
  alert: global.alert,
  console: console,
  currentSessionId: 'sess_dom_test',
  currentCsrfToken: 'csrf_mock_token_128bits',
  appendChatMessage: (role, text) => {},
  renderSendAggregatorCard: async (id) => {}
};
vm.createContext(context);
vm.runInContext(scriptContent, context);
vm.runInContext("currentSessionId = 'sess_dom_test'; currentCsrfToken = 'csrf_mock_token_128bits';", context);

// ── TEST EXECUTION ──────────────────────────────────────────────────────────
const results = [];

function assert(condition, message) {
  if (!condition) {
    throw new Error(`ASSERTION FAILED: ${message}`);
  }
  results.push(`PASS: ${message}`);
  console.log(`PASS: ${message}`);
}

try {
  console.log('=== RUNNING DOM CAROUSEL & APPROVAL HARNESS ===');

  const sample3Templates = [
    {
      message_index: 1,
      stage: 'Launch',
      tone: 'Urgent & Direct',
      body: 'Message 1 Body text. Reply STOP to unsubscribe.',
      word_count: 8,
      word_limit: 320,
      compliant: true,
      previews: []
    },
    {
      message_index: 2,
      stage: 'Reminder',
      tone: 'Urgent & Direct',
      body: 'Message 2 Body text. Reply STOP to unsubscribe.',
      word_count: 8,
      word_limit: 320,
      compliant: true,
      previews: []
    },
    {
      message_index: 3,
      stage: 'Final Call',
      tone: 'Urgent & Direct',
      body: 'Message 3 Body text. Reply STOP to unsubscribe.',
      word_count: 8,
      word_limit: 320,
      compliant: true,
      previews: []
    }
  ];

  // 1. Initial Render
  context.renderTemplates(sample3Templates, 'Day 1 -> Day 3 -> Day 5');
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 1 of 3', 'Initial state displays Message 1 of 3');
  assert(document.getElementById('prevTemplateBtn').disabled === true, 'Previous button is initially disabled at index 0');
  assert(document.getElementById('nextTemplateBtn').disabled === false, 'Next button is enabled at index 0');
  assert(document.getElementById('templatesList').innerHTML.includes('Message 1 (Launch)'), 'Slide 1 content is rendered in templatesList');

  // 2. Click Next -> Slide 2
  context.nextTemplateSlide();
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 2 of 3', 'Clicking Next displays Message 2 of 3');
  assert(document.getElementById('prevTemplateBtn').disabled === false, 'Previous button is now enabled at index 1');
  assert(document.getElementById('nextTemplateBtn').disabled === false, 'Next button is enabled at index 1');
  assert(document.getElementById('templatesList').innerHTML.includes('Message 2 (Reminder)'), 'Slide 2 content is rendered');

  // 3. Click Next again -> Slide 3
  context.nextTemplateSlide();
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 3 of 3', 'Clicking Next displays Message 3 of 3');
  assert(document.getElementById('prevTemplateBtn').disabled === false, 'Previous button is enabled at index 2');
  assert(document.getElementById('nextTemplateBtn').disabled === true, 'Next button is disabled at final slide (index 2 of 3)');
  assert(document.getElementById('templatesList').innerHTML.includes('Message 3 (Final Call)'), 'Slide 3 content is rendered');

  // 4. Click Prev -> Goes back to Slide 2
  context.prevTemplateSlide();
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 2 of 3', 'Previous button returns to Message 2 of 3');

  // 5. Keyboard Navigation
  const leftEvent = { key: 'ArrowLeft' };
  keyListeners.forEach(l => l(leftEvent));
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 1 of 3', 'ArrowLeft keyboard navigation returns to Message 1 of 3');

  const rightEvent = { key: 'ArrowRight' };
  keyListeners.forEach(l => l(rightEvent));
  assert(document.getElementById('templateCounterBadge').textContent === 'Message 2 of 3', 'ArrowRight keyboard navigation advances to Message 2 of 3');

  // 6. Approve Button Interaction
  const approveBtn = document.getElementById('approveCampaignBtn');
  assert(approveBtn.disabled === false, 'Approve Campaign button is enabled before click');

  // Call approve
  context.approveCampaign().then(() => {
    assert(fetchCalls.length > 0, 'Approve button triggered fetch call');
    const approveCall = fetchCalls.find(c => c.url === '/api/campaign/approve');
    assert(!!approveCall, 'Approve called /api/campaign/approve');
    assert(approveCall.opts.method === 'POST', 'Approve request used POST method');
    assert(approveCall.opts.headers['X-CSRF-Token'] === 'csrf_mock_token_128bits', 'Approve passed X-CSRF-Token header');
    const bodyObj = JSON.parse(approveCall.opts.body);
    assert(bodyObj.csrf_token === 'csrf_mock_token_128bits', 'Approve body includes csrf_token');
    assert(bodyObj.session_id === 'sess_dom_test', 'Approve body includes session_id');
    assert(approveBtn.innerHTML.includes('Approved'), 'Approve button transitions to Approved text');

    // Save evidence
    const output = {
      test: 'DOM-level Carousel & Approve Verification',
      timestamp: new Date().toISOString(),
      results: results,
      all_passed: true
    };
    fs.writeFileSync(path.join(__dirname, '..', '.apitool', 'evidence', 'dom_carousel_test_result.json'), JSON.stringify(output, null, 2));
    console.log('\nALL 14 DOM-LEVEL CAROUSEL & APPROVE ASSERTIONS PASSED!');
    process.exit(0);
  }).catch(err => {
    console.error('Approve failed:', err);
    process.exit(1);
  });

} catch (e) {
  console.error(e.message);
  process.exit(1);
}
