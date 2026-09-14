// .github/scripts/create-release.js
//
// Creates or updates the GitHub Release for a published image. Called from the
// release workflow via actions/github-script:
//
//   await require('./.github/scripts/create-release.js')({ github, context });
//
// Inputs (env): VERSION, IMAGE_NAME, ARCHES (space-separated, e.g. "amd64 arm64").
// Expects per architecture, as downloaded by the workflow:
//   trivy-reports/trivy-findings-<arch>/trivy-<arch>.json    (attached as asset)
//   trivy-reports/trivy-findings-<arch>/trivy-<arch>.status  (one-line verdict)
//
// A new release is created as a draft and only published once every asset is
// attached, so a public release never links to a report that is missing.

'use strict';

const fs = require('node:fs');
const path = require('node:path');

function requireEnv(name) {
  const value = (process.env[name] || '').trim();
  if (!value) throw new Error(`Missing required environment variable ${name}`);
  return value;
}

// "v1.4" -> "1.4.0", "1.2-rc.1" -> "1.2.0-rc.1". The prerelease suffix is kept:
// 1.2.0-rc.1 and 1.2.0 are different releases.
function normalizeVersion(value) {
  const [main, ...suffix] = value.replace(/^v/, '').split('-');
  const parts = main.split('.');
  while (parts.length < 3) parts.push('0');
  return [parts.join('.'), ...suffix].join('-');
}

function extractVersionSection(markdown, targetVersion) {
  const headings = [...markdown.matchAll(/^## \[([^\]]+)\] - .+$/gm)];
  const exact = (label) => label === targetVersion || label === 'v' + targetVersion;
  const normalized = (label) => normalizeVersion(label) === normalizeVersion(targetVersion);

  // An exact heading always wins over a merely equivalent one ("1.4" vs "1.4.0").
  let index = headings.findIndex((m) => exact(m[1]));
  if (index === -1) index = headings.findIndex((m) => normalized(m[1]));
  if (index === -1) return null;

  const start = headings[index].index + headings[index][0].length;
  const end = index + 1 < headings.length ? headings[index + 1].index : markdown.length;
  // The older versions sit in a <details> block: cut at its opening (newest
  // section) or closing (last section) tag.
  return markdown.slice(start, end).split(/<\/?details\b/)[0].trim();
}

function readChangelogSection(version) {
  try {
    const section = extractVersionSection(fs.readFileSync('CHANGELOG.md', 'utf8'), version);
    if (!section) console.log(`No matching changelog section found for version ${version}`);
    return section;
  } catch (err) {
    console.log(`Could not read CHANGELOG.md: ${err.message}`);
    return null;
  }
}

module.exports = async ({ github, context }) => {
  const version = requireEnv('VERSION');
  const imageName = requireEnv('IMAGE_NAME');
  const arches = requireEnv('ARCHES').split(/\s+/).sort();
  const { owner, repo } = context.repo;
  const isPrerelease = version.includes('-');
  const tagName = `v${version}`;
  const runUrl = `https://github.com/${owner}/${repo}/actions/runs/${context.runId}`;
  const assetBase = `https://github.com/${owner}/${repo}/releases/download/${tagName}`;

  // Fail before touching the release if a report or its verdict is missing.
  const reports = arches.map((arch) => {
    const dir = path.join('trivy-reports', `trivy-findings-${arch}`);
    const assetName = `trivy-${arch}.json`;
    return {
      arch,
      assetName,
      data: fs.readFileSync(path.join(dir, assetName)),
      verdict: fs.readFileSync(path.join(dir, `trivy-${arch}.status`), 'utf8').trim(),
    };
  });

  const platformNote = arches.length > 1
    ? `multi-arch: ${arches.map((a) => `linux/${a}`).join(', ')}`
    : `linux/${arches[0]}`;

  const body = [
    `## fritzFluxDB ${version}`,
    '',
    `**Docker Image:** \`docker pull ${imageName}:${version}\``,
    '',
    '### Available Tags',
    `- \`${imageName}:${version}\` (${platformNote})`,
    isPrerelease
      ? '- `latest` unchanged: prereleases do not move it.'
      : `- \`${imageName}:latest\` (${platformNote})`,
    '',
    '### Security Scan Results',
    '',
    '| Architecture | Status | Findings |',
    '|---|---|---|',
    ...reports.map((r) => `| ${r.arch} | ${r.verdict} | [${r.assetName}](${assetBase}/${r.assetName}) |`),
    `| run | workflow details | [Actions Run](${runUrl}) |`,
    '',
    '> Scanned with [Trivy](https://trivy.dev/): HIGH and CRITICAL findings that have a fix available; ' +
      'unfixed CVEs and entries in `.trivyignore` are not reported.',
    '',
    '### Changelog',
    readChangelogSection(version) || '_No matching CHANGELOG entry found for this version._',
  ].join('\n');

  const releaseFields = {
    owner,
    repo,
    tag_name: tagName,
    name: `fritzFluxDB ${tagName}`,
    body,
    prerelease: isPrerelease,
  };

  // listReleases also returns drafts (getReleaseByTag does not), so a draft
  // left behind by an interrupted run is reused instead of duplicated.
  const releases = await github.paginate(github.rest.repos.listReleases, { owner, repo, per_page: 100 });
  let release = releases.find((r) => r.tag_name === tagName);

  if (!release) {
    // target_commitish pins a not-yet-existing tag to the commit this run
    // built; without it GitHub would create the tag on the default branch.
    ({ data: release } = await github.rest.repos.createRelease({
      ...releaseFields,
      target_commitish: context.sha,
      draft: true,
    }));
    console.log(`Created draft release ${tagName}`);
  }

  // Run artifacts expire after retention-days; attaching the scan reports as
  // release assets keeps the links in the body valid for as long as the
  // release exists. Each report is uploaded under a staging name first, so an
  // existing asset is only replaced once its successor is in place.
  for (const report of reports) {
    const stagingName = report.assetName + '.staging';
    const existingAssets = release.assets || [];

    for (const stale of existingAssets.filter((a) => a.name === stagingName)) {
      await github.rest.repos.deleteReleaseAsset({ owner, repo, asset_id: stale.id });
    }

    const { data: uploaded } = await github.rest.repos.uploadReleaseAsset({
      owner,
      repo,
      release_id: release.id,
      name: stagingName,
      data: report.data,
      headers: {
        'content-type': 'application/json',
        'content-length': report.data.length,
      },
    });

    for (const previous of existingAssets.filter((a) => a.name === report.assetName)) {
      await github.rest.repos.deleteReleaseAsset({ owner, repo, asset_id: previous.id });
    }
    await github.rest.repos.updateReleaseAsset({
      owner,
      repo,
      asset_id: uploaded.id,
      name: report.assetName,
    });
    console.log(`Attached ${report.assetName}`);
  }

  await github.rest.repos.updateRelease({
    ...releaseFields,
    release_id: release.id,
    // A draft's tag does not exist yet; it is created on publish, at the commit this run built.
    ...(release.draft ? { target_commitish: context.sha } : {}),
    draft: false,
  });
  console.log(`Published release ${tagName}`);
};

// Exported for tests/test_release_changelog.py.
module.exports.extractVersionSection = extractVersionSection;
