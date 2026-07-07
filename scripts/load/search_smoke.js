// k6 load test for the catalog read path (staging-ready).
//
//   BASE_URL=https://staging.example.test ORG=metro-library \
//     k6 run scripts/load/search_smoke.js
//
// Models a realistic session: search -> click a result (work detail). Metrics
// are tagged per endpoint and thresholds are aligned to docs/policies/slos.md.
//
// NOTE: run against an environment whose API throttle is raised for the load
// source, or you will measure the rate limiter, not the app:
//   THROTTLE_ANON=100000/minute  (see elibrary/settings.py)
import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = (__ENV.BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const ORG = __ENV.ORG || "";
const ORGQ = ORG ? `&org=${ORG}` : "";
const ORGQ1 = ORG ? `?org=${ORG}` : "";
const TERMS = ["archive", "history", "science", "world", "art", "life", "space", "garden", "river", "the"];

const searchDuration = new Trend("search_duration", true);
const detailDuration = new Trend("detail_duration", true);

export const options = {
  stages: [
    { duration: "30s", target: 10 },
    { duration: "1m", target: 50 },
    { duration: "30s", target: 0 },
  ],
  thresholds: {
    // SLOs (docs/policies/slos.md): search + detail p95 < 400ms; <0.1% failures.
    http_req_failed: ["rate<0.001"],
    search_duration: ["p(95)<400"],
    detail_duration: ["p(95)<400"],
  },
};

function pick(a) {
  return a[Math.floor(Math.random() * a.length)];
}

export default function () {
  const q = pick(TERMS);

  const s = http.get(`${BASE_URL}/api/v1/catalog/search/?q=${q}${ORGQ}`, { tags: { name: "search" } });
  searchDuration.add(s.timings.duration);
  check(s, { "search 200": (r) => r.status === 200 });

  // Click through to the first result, like a real user.
  let slug = null;
  try {
    const data = s.json("data");
    if (data && data.length) slug = data[0].slug;
  } catch (e) {
    slug = null;
  }
  if (slug) {
    const d = http.get(`${BASE_URL}/api/v1/catalog/works/${slug}/${ORGQ1}`, { tags: { name: "detail" } });
    detailDuration.add(d.timings.duration);
    check(d, { "detail 200": (r) => r.status === 200 });
  }

  sleep(1);
}
