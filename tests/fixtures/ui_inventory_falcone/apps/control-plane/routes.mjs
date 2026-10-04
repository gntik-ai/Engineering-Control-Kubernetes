// Seed route table (fixture).
const PO = '/repo/packages/provisioning-orchestrator/src/actions';

export const routes = [
  { method: 'GET',  path: '/v1/plans', module: `${PO}/plan-list.mjs`, export: 'main' },
  { method: 'POST', path: '/v1/plans', module: `${PO}/plan-create.mjs`, export: 'main' },
  { method: 'GET', path: '/v1/plans/{planId}', module: `${PO}/plan-get.mjs`, export: 'main' },
  // DELETE is routed but not published.
  { method: 'DELETE', path: '/v1/plans/{planId}', module: `${PO}/plan-delete.mjs`, export: 'main' },
];
