// k6 load/smoke test for the catalog search read path.
//   BASE_URL=https://staging.example.test k6 run scripts/load/search_smoke.js
import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const TERMS = ["archive", "dune", "history", "python", "garden", "space"];

export const options = {
  stages: [
    { duration: "30s", target: 10 },
    { duration: "1m", target: 50 },
    { duration: "30s", target: 0 },
  ],
  thresholds: {
    // Align with docs/policies/slos.md.
    http_req_duration: ["p(95)<400"],
    http_req_failed: ["rate<0.001"],
  },
};

export default function () {
  const q = TERMS[Math.floor(Math.random() * TERMS.length)];
  const res = http.get(`${BASE_URL}/api/v1/catalog/search/?q=${q}`);
  check(res, {
    "status is 200": (r) => r.status === 200,
    "has results field": (r) => r.body && r.body.includes("result_count"),
  });
  sleep(1);
}
