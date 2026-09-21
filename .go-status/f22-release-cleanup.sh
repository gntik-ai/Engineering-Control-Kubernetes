#!/usr/bin/env bash
# F-22: delete the 46 oldest release directories under /opt/engineering-control/releases,
# keeping the 15 most recent (including the current live release, 2b678d9...). All 61
# existing release directories were confirmed git-reachable on 2026-09-21 (via
# `git cat-file -e <hash>` against /home/hermes/engineering/engineering-control) before
# this list was generated, so none of these deletions lose any source history.
#
# This script only exists because the harness's own auto-mode classifier blocks this
# session from running rm against /opt/engineering-control/releases -- even a single
# directory, even just listing files for deletion -- as "[Irreversible Deletion]". That
# is a deliberate safety boundary, not a bug: the operator runs this themselves.
#
# Re-verify before running (state may have changed since 2026-09-21):
#   - none of these hashes is the current live release: readlink -f /opt/engineering-control/source
#   - each hash is still git-reachable: cd /home/hermes/engineering/engineering-control && git cat-file -e <hash>
set -euo pipefail

DELETE_HASHES=(
  05a3ac31ed86c43507d33b6792d1373b20d089c4
  088a7b70d669a7ef0e3b22eb36fb8457e0b10b6b
  19dcdafe9ad34334e031656207d05e903a4302d8
  1d4139b871d234fd2836e8c1bfbb421fa663715b
  25badb79716adeb7c58db04dedf2e55189596253
  30a0e8615edc303ff96c2efcf718ed6a46d8c3ef
  3bfc0e6fd85173e7198c494979203f03df3e24d2
  4a30f6acc73a35723ce9b7d5e336a5fd670f8166
  4e619464b13ac9ed65fb60a4c8043091597301c5
  53d3c171902cf150f68ba5221ab768fe55b63854
  768ef2c1a1a0507b3ffffb875c42b0988de1bfa3
  774124815c40c54408174db25c4bcdd119b99149
  7d2e9d2203f8d542f1dfc2111a09226f538e9324
  83159f1ac34fbe0a23ad8a3a5bd3e2362894836a
  842e0ff0e1d1a8908bf7785bf6e5d78e5d112a36
  8de79c837548a889107d20ddac8828fbfb96dbcd
  91b6295be13d7cfd374f0045b04f4e808a125b9d
  9a97ab89bd509c380d17f4b9901f17a24d99d6bb
  a08d0966054fe6b4274abef14f58abc9f4fea2b0
  a3670a540aa3a53966a198373ca457fc86a7fe4e
  a4e0f72a5e2ff3734557a4fb641bfff21b0f3123
  a4ea1a775fb54f0d45858183e78c24c59e3c9a23
  bbabdbf4563dc79d1196032893e8abdc5ff4c473
  be33e08f1011db9b67cd50bd06e7c4aa6031c6fd
  c09b495e22ab83fd6e09941548ff2a0dac9f4da6
  cd820fa0cbcd34e06cbf6582e4e602cf8477dc03
  cdb21009723cd2a2ecba0e26dbbdb64a6567aaf8
  cec65fa9825b2e2c8e4cc254bbe5405e6864e67f
  ddbce2c70865669354d7f5e81dbe215fbaa22c0c
  dff15c4b40bfdc39be062d2837d40500c473992e
  f063b897a7252c81f602bd3e667090230dac9a83
  f45612f3d27f365e811907e71382ac4dd2bf8a5b
  fe1c2f7ad8bf55e3f93e039be949a6efde50455e
  146d67e99a8b174268196eb11e51eb1030bca9dc
  d36cc6551e80585b7c40a968095ff4aaae6c9987
  1bcf114a8f554b9fcd17927c2bf3c9d03b90beb7
  e5aeb84fd959ffc21a2aa19512371a9a91393581
  e907c4056b2bb16acb6082f1743456a94b0030f9
  10af445f37b71a7105e7d0030201e96ebae1afc9
  959ac5cd5e2bfd0afeb8b41cc923318246839e2e
  c9228b7ea80aeed53497bf872aa95d8135281f07
  ec93676dfbb6cf045bd3818d311a72cdee73f731
  1e75be57725c05b45d0a8de52a57b758723b04aa
  249aa6830301132fbe3c22695d3557c2cbe333c8
  83a48ccb2058aa712c5ca83a71090c894ca93c11
  ce69122885c0fd07a3685daae2d9c038583eaec6
)

RELEASES_ROOT=/opt/engineering-control/releases
LIVE=$(readlink -f /opt/engineering-control/source | sed -E "s#$RELEASES_ROOT/([^/]+)/.*#\1#")
echo "Current live release: $LIVE"

for hash in "${DELETE_HASHES[@]}"; do
  if [[ "$hash" == "$LIVE" ]]; then
    echo "REFUSING to delete $hash -- it is the current live release. Skipping." >&2
    continue
  fi
  if [[ ! -d "$RELEASES_ROOT/$hash" ]]; then
    echo "Skipping $hash -- directory already gone."
    continue
  fi
  echo "Deleting $RELEASES_ROOT/$hash"
  sudo rm -rf "${RELEASES_ROOT:?}/${hash:?}"
done

echo "Done. Remaining release directories:"
ls -1 "$RELEASES_ROOT"
