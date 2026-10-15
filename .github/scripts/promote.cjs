module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const repository = `${owner}/${repo}`;
  if (context.eventName !== 'push' || context.ref !== 'refs/heads/dev' ||
      context.payload.repository.full_name !== repository) return;
  const api = { owner, repo };
  const current = async () => (await github.rest.git.getRef({ ...api, ref: 'heads/dev' })).data.object.sha;
  if (await current() !== context.sha) return;
  const graded = (await github.rest.repos.getCommit({ ...api, ref: context.sha })).data;
  let main = (await github.rest.repos.getCommit({ ...api, ref: 'main' })).data;
  if (main.commit.tree.sha !== graded.commit.tree.sha) {
    const pulls = (await github.rest.pulls.list({ ...api, state: 'open', base: 'main', head: `${owner}:dev` })).data;
    const pull = pulls[0] || (await github.rest.pulls.create({
      ...api, base: 'main', head: 'dev', title: 'Promote green dev to main',
      body: `Graded dev revision: ${context.sha}\n\nTests: ${context.serverUrl}/${repository}/actions/runs/${context.runId}`,
    })).data;
    if (pull.head.sha !== context.sha || pull.head.repo.full_name !== repository || pull.base.ref !== 'main') {
      throw new Error('Promotion pull request does not match the graded dev head');
    }
    if (await current() !== context.sha) return;
    const merged = (await github.rest.pulls.merge({ ...api, pull_number: pull.number, sha: context.sha, merge_method: 'rebase' })).data;
    if (!merged.merged) throw new Error('GitHub refused the promotion merge');
    main = (await github.rest.repos.getCommit({ ...api, ref: merged.sha })).data;
    if (main.commit.tree.sha !== graded.commit.tree.sha) throw new Error('Promoted tree differs from the graded dev tree');
    core.setOutput('pull-request', pull.html_url);
  }
  core.setOutput('graded', context.sha);
  core.setOutput('main', main.sha);
  await core.summary.addHeading('Promotion').addTable([
    ['Graded dev', context.sha], ['Promoted main', main.sha], ['Tree', graded.commit.tree.sha],
  ]).write();
};
