// Run with: node --test tests/frontend_release.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '..', 'app.py'), 'utf8');
const script = source.split('<script>')[1].split('</script>')[0];

function harness() {
  const elements = new Map();
  const pending = [];
  const alerts = [];
  function element() {
    return {style: {}, classList: {toggle(){}, add(){}, remove(){}}, dataset: {},
      value: '', checked: false, innerText: '', innerHTML: '', appendChild(){},
      addEventListener(){}, setAttribute(){}};
  }
  class FormData { append() {} }
  const context = vm.createContext({
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      querySelectorAll: () => [], createElement: element,
    },
    window: {addEventListener(){}}, FormData,
    fetch: (url, args) => new Promise((resolve, reject) => pending.push({url, args, resolve, reject})),
    alert: message => alerts.push(message),
    setTimeout: () => 0, clearTimeout(){}, setInterval: () => 0, clearInterval(){},
  });
  vm.runInContext(script, context);
  const run = code => vm.runInContext(code, context);
  const reply = (i, data, status = 200) => pending[i].resolve({ok: status >= 200 && status < 300,
    status, json: async () => data});
  const preview = (page, name) => ({page_num: page, total_pages: 10, kpw6_pages: [],
    slices_meta: [], source_annotated: name});
  return {elements, pending, alerts, run, reply, preview};
}

test('closing and reopening the lightbox rejects the prior HD image', async () => {
  const h = harness();
  const first = h.run('openLightbox("page1-low", "Page 1", "source", 0)');
  h.run('closeLightbox(); currentPage = 5');
  const second = h.run('openLightbox("page6-low", "Page 6", "source", 0)');
  h.reply(1, {hd_image: 'page6-HD', width: 2142, height: 3032});
  await second;
  h.reply(0, {hd_image: 'page1-HD', width: 2142, height: 3032});
  await first;
  assert.equal(h.elements.get('lightboxImg').src, 'page6-HD');
  assert.equal(h.elements.get('lightboxTitle').innerText, '🔍 Page 6');
});

test('stale HD errors cannot change the active lightbox status', async () => {
  const h = harness();
  const first = h.run('openLightbox("screen1", "Screen 1", "kpw6", 0)');
  const second = h.run('openLightbox("source", "Source", "source", 0)');
  h.reply(1, {hd_image: 'source-HD', width: 2142, height: 3032});
  await second;
  const activeStatus = h.elements.get('lightboxHdBadge').innerText;
  h.reply(0, {error: 'old request failed'}, 400);
  await first;
  assert.equal(h.elements.get('lightboxHdBadge').innerText, activeStatus);
  assert.equal(h.elements.get('lightboxImg').src, 'source-HD');
});

test('an HD response after close cannot modify the closed lightbox', async () => {
  const h = harness();
  const request = h.run('openLightbox("low", "Page 1", "source", 0)');
  h.run('closeLightbox()');
  const statusAtClose = h.elements.get('lightboxHdBadge').innerText;
  h.pending[0].reject(new Error('connection closed'));
  await request;
  assert.equal(h.elements.get('lightboxHdBadge').innerText, statusAtClose);
  assert.equal(h.elements.get('lightboxModal').style.display, 'none');
});

test('the newest selected custom cover wins when uploads finish out of order', async () => {
  const h = harness();
  h.run('currentCoverMode = "custom_image"');
  const first = h.run('handleCoverUpload({name: "A.png"})');
  const second = h.run('handleCoverUpload({name: "B.png"})');
  h.reply(1, {cover_id: 'cover-B', filename: 'B.png'});
  await second;
  h.reply(0, {cover_id: 'cover-A', filename: 'A.png'});
  await first;
  assert.equal(h.run('currentCustomCoverId'), 'cover-B');
  assert.match(h.elements.get('coverUploadStatus').innerText, /B\.png/);
  const payload = JSON.parse(h.pending.at(-1).args.body);
  assert.equal(payload.custom_cover_id, 'cover-B');
});

test('a failed cover upload preserves the previous cover and reports the failure', async () => {
  const h = harness();
  h.run('currentCustomCoverId = "previous-good-cover"');
  const request = h.run('handleCoverUpload({name: "corrupt.png"})');
  h.reply(0, {error: '图片损坏'}, 400);
  await request;
  assert.equal(h.run('currentCustomCoverId'), 'previous-good-cover');
  assert.match(h.elements.get('coverUploadStatus').innerText, /失败.*图片损坏/);
  assert.equal(h.pending.length, 1, 'Failed upload must not refresh using a bad cover');
});

test('an older failed cover upload cannot replace the newest upload status', async () => {
  const h = harness();
  const first = h.run('handleCoverUpload({name: "A.png"})');
  const second = h.run('handleCoverUpload({name: "B.png"})');
  h.reply(1, {cover_id: 'cover-B', filename: 'B.png'});
  await second;
  const status = h.elements.get('coverUploadStatus').innerText;
  h.pending[0].reject(new Error('A failed'));
  await assert.doesNotReject(first);
  assert.equal(h.run('currentCustomCoverId'), 'cover-B');
  assert.equal(h.elements.get('coverUploadStatus').innerText, status);
});

test('startup history does not override a later explicit document selection', async () => {
  const h = harness();
  const init = h.run('fetchUploadedFilesAndInit()');
  const selected = h.run('loadDemo("demo_tech_book", "tech_book")');
  h.reply(1, {highlights: []});
  await selected;
  h.reply(0, {uploaded: [{file_id: 'last-uploaded', name: 'last-uploaded.pdf'}]});
  await new Promise(setImmediate);
  // Complete a buggy initialization path too, so the failure is a selection assertion.
  if (h.pending[3]) {
    h.reply(3, {highlights: []});
    await new Promise(setImmediate);
    h.reply(4, h.preview(0, 'last-uploaded'));
  }
  await init;
  assert.equal(h.run('currentFileId'), 'demo_tech_book');
});

test('startup history does not invalidate a newer PDF upload', async () => {
  const h = harness();
  const init = h.run('fetchUploadedFilesAndInit()');
  const upload = h.run('handleFileUpload({name: "new.pdf"})');
  h.reply(0, {uploaded: []});
  await init;
  h.reply(1, {file_id: 'new-book', filename: 'new.pdf', highlights: []});
  await upload;
  assert.equal(h.run('currentFileId'), 'new-book');
  assert.equal(h.pending.filter(p => p.url.startsWith('/api/doc_info')).length, 0);
});

test('startup still opens the latest history entry when no user selection intervenes', async () => {
  const h = harness();
  const init = h.run('fetchUploadedFilesAndInit()');
  h.reply(0, {uploaded: [{file_id: 'latest-book', name: 'latest.pdf'}]});
  await new Promise(setImmediate);
  h.reply(1, {highlights: []});
  await new Promise(setImmediate);
  h.reply(2, h.preview(0, 'latest-book'));
  await init;
  assert.equal(h.run('currentFileId'), 'latest-book');
  assert.equal(h.elements.get('sourcePreviewImg').src, 'latest-book');
});

test('the current preview error clears stale images and gives an explicit message', async () => {
  const h = harness();
  h.run('currentPage = 5; currentScreens = ["old-screen"]; currentSlicesMeta = [{index: 1}];' +
    'document.getElementById("sourcePreviewImg").src = "old-source";' +
    'document.getElementById("singleKpw6Img").src = "old-screen"');
  const preview = h.run('refreshPreview()');
  h.reply(0, {error: '无法处理该页面'}, 400);
  await preview;
  assert.equal(h.elements.get('sourcePreviewImg').src, '');
  assert.equal(h.elements.get('singleKpw6Img').src, '');
  assert.equal(h.run('currentScreens.length'), 0);
  assert.equal(h.run('currentSlicesMeta.length'), 0);
  assert.match(h.elements.get('previewErrorNotice').innerText, /预览失败.*无法处理该页面/);
  assert.notEqual(h.elements.get('previewErrorNotice').style.display, 'none');
});

test('a rejected PDF upload preserves an in-flight preview for the current valid document', async () => {
  const h = harness();
  h.run('currentFileId = "valid-book"; document.getElementById("sourcePreviewImg").src = "older-image"');
  const preview = h.run('refreshPreview()');
  const upload = h.run('handleFileUpload({name: "corrupt.pdf"})');
  h.reply(1, {error: 'PDF损坏'}, 400);
  await upload;
  h.reply(0, h.preview(0, 'valid-book-image'));
  await preview;
  assert.equal(h.run('currentFileId'), 'valid-book');
  assert.equal(h.elements.get('sourcePreviewImg').src, 'valid-book-image');
  assert.match(h.alerts.at(-1), /PDF损坏/);
});

test('a stale preview error cannot clear a newer successful preview', async () => {
  const h = harness();
  const first = h.run('refreshPreview()');
  h.run('currentPage = 5');
  const second = h.run('refreshPreview()');
  h.reply(1, h.preview(5, 'page6'));
  await second;
  h.reply(0, {error: 'old page failed'}, 400);
  await first;
  assert.equal(h.elements.get('sourcePreviewImg').src, 'page6');
  assert.notEqual(h.elements.get('previewErrorNotice')?.style.display, 'block');
});

test('a current preview connection failure clears the image without an unhandled rejection', async () => {
  const h = harness();
  h.run('document.getElementById("sourcePreviewImg").src = "old-source"');
  const preview = h.run('refreshPreview()');
  h.pending[0].reject(new Error('连接中断'));
  await assert.doesNotReject(preview);
  assert.equal(h.elements.get('sourcePreviewImg').src, '');
  assert.match(h.elements.get('previewErrorNotice').innerText, /连接中断/);
});

test('POST requests include X-KPW6-Token while GET requests omit request options', async () => {
  const h = harness();
  const preview = h.run('refreshPreview()');
  assert.equal(h.pending[0].args.headers['X-KPW6-Token'], '__KPW6_CSRF_TOKEN__');
  h.reply(0, h.preview(0, 'ok'));
  await preview;

  const init = h.run('fetchUploadedFilesAndInit()');
  assert.equal(h.pending[1].args, undefined);
  h.reply(1, {uploaded: []});
  await new Promise(setImmediate);
  if (h.pending[2]) h.reply(2, {highlights: []});
  await new Promise(setImmediate);
  if (h.pending[3]) h.reply(3, h.preview(0, 'demo'));
  await init;
});

test('cancelling an active export hides the progress bar without an error alert', async () => {
  const h = harness();
  const exp = h.run('exportSelectedFormat("pdf")');
  const exportIdx = h.pending.findIndex(p => p.url === '/api/export_unified');
  assert.notEqual(exportIdx, -1);
  const cancel = h.run('cancelActiveExport()');
  const cancelIdx = h.pending.findIndex(p => p.url === '/api/cancel_export');
  assert.notEqual(cancelIdx, -1);
  h.reply(cancelIdx, {ok: true, cancelled: true});
  await cancel;
  h.reply(exportIdx, {cancelled: true});
  await exp;
  assert.equal(h.alerts.length, 0);
  assert.equal(h.elements.get('exportProgressPanel').style.display, 'none');
});

test('deleting the active uploaded file switches cleanly to remaining history or demo', async () => {
  const h = harness();
  h.run('currentFileId = "book1"');
  const del = h.run('deleteUploadedFile("book1")');
  h.reply(0, {ok: true, uploaded: []});
  await new Promise(setImmediate);
  if (h.pending[1]) h.reply(1, {highlights: []});
  await new Promise(setImmediate);
  if (h.pending[2]) h.reply(2, h.preview(0, 'demo-fallback'));
  await del;
  assert.equal(h.run('currentFileId'), 'demo_academic_2col');
});
