import assert from "node:assert/strict";
import test from "node:test";

import { renderDirectionalGeneMarkdown } from "./markdown.js";

test("renders accessible UP and DOWN gene labels", () => {
  const html = renderDirectionalGeneMarkdown("[[up:DUOX2]] and [[down:ATP2B3]]");

  assert.match(html, /gene-direction-up/);
  assert.match(html, /UP .*DUOX2/);
  assert.match(html, /gene-direction-down/);
  assert.match(html, /DOWN .*ATP2B3/);
});

test("supports punctuation used in gene and feature identifiers", () => {
  const html = renderDirectionalGeneMarkdown("[[up:HLA-DRA]]; [[down:LOC654433.2]]");

  assert.match(html, /HLA-DRA/);
  assert.match(html, /LOC654433\.2/);
});

test("renders portable colored gene spans after Markdown HTML escaping", () => {
  const html = renderDirectionalGeneMarkdown(
    "&lt;span style=&quot;color:#B42318&quot;&gt;↑ HLA-DRA&lt;/span&gt; and &lt;span style=&quot;color:#175CD3&quot;&gt;↓ LOC654433.2&lt;/span&gt;",
  );

  assert.match(html, /gene-direction-up/);
  assert.match(html, /UP .*HLA-DRA/);
  assert.match(html, /gene-direction-down/);
  assert.match(html, /DOWN .*LOC654433\.2/);
});

test("does not treat arbitrary HTML or mismatched direction spans as safe markup", () => {
  const html = renderDirectionalGeneMarkdown(
    "&lt;span style=&quot;color:green&quot;&gt;↑ DUOX2&lt;/span&gt; &lt;span style=&quot;color:#B42318&quot;&gt;↓ ATP2B3&lt;/span&gt;",
  );

  assert.doesNotMatch(html, /gene-direction-(?:up|down)/);
  assert.match(html, /color:green/);
  assert.match(html, /color:#B42318/);
});

test("does not restyle literal tokens inside code spans or link attributes", () => {
  const html = renderDirectionalGeneMarkdown(
    '<code>[[up:DUOX2]]</code> <a href="https://example.test/[[down:ATP2B3]]">[[down:ATP2B3]]</a>',
  );

  assert.match(html, /<code>\[\[up:DUOX2\]\]<\/code>/);
  assert.match(html, /href="https:\/\/example\.test\/\[\[down:ATP2B3\]\]"/);
  assert.match(html, /gene-direction-down/);
});

test("does not restyle portable spans inside code or link attributes", () => {
  const html = renderDirectionalGeneMarkdown(
    '<code>&lt;span style=&quot;color:#B42318&quot;&gt;↑ DUOX2&lt;/span&gt;</code> <a href="https://example.test/?gene=&lt;span style=&quot;color:#175CD3&quot;&gt;↓ ATP2B3&lt;/span&gt;">example</a>',
  );

  assert.doesNotMatch(html, /gene-direction-(?:up|down)/);
  assert.match(html, /<code>&lt;span style=&quot;color:#B42318&quot;&gt;↑ DUOX2&lt;\/span&gt;<\/code>/);
  assert.match(html, /href="https:\/\/example\.test\/\?gene=&lt;span style=&quot;color:#175CD3&quot;&gt;↓ ATP2B3&lt;\/span&gt;"/);
});
