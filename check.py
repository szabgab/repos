import argparse
import datetime
import fcntl
import json
import yaml
import logging
import os
import re
import shlex
from mytools import cwd, capture2

git = 'git'

def _system(cmd):
    logger = logging.getLogger(__name__)

    if type(cmd).__name__ == 'list':
        cmd_list = cmd
        cmd_str = ' '.join(cmd)
    elif type(cmd).__name__ == 'str':
        cmd_list = shlex.split(cmd)
        cmd_str = cmd
    else:
        raise Exception("Invalid paramerer type: " + type(cmd).__name__)

    logger.debug(cmd_str)
    code, out = capture2(cmd_list)
    logger.debug("Exit code for '{}' is '{}'. Output is {}.".format(cmd_str, code, out))
    return code, out

class CI(object):
    def setup_logger(self):
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.DEBUG)

        root = os.path.dirname(os.path.abspath(__file__))
        logdir = os.path.join(root, 'logs')
        if not os.path.exists(logdir):
            os.mkdir(logdir)

        fh = logging.FileHandler(os.path.join(logdir, datetime.datetime.now().strftime("%Y-%m-%d.log")))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter( logging.Formatter('%(asctime)s - %(name)s - %(levelname)-10s - %(message)s') )
        logger.addHandler(fh)

    def add_build_logger(self, build_directory):
        logger = logging.getLogger(__name__)

        fh = logging.FileHandler(os.path.join(build_directory, 'ci.log'))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter( logging.Formatter('%(asctime)s - %(name)s - %(levelname)-10s - %(message)s') )
        logger.addHandler(fh)


    def add_logger(self):
        logger = logging.getLogger(__name__)

        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)-10s - %(message)s'))
        logger.addHandler(sh)

    # given path to a repository return a dictionary where the keys are branch names
    # the values are the sha1 of each branch
    def get_branches(self, path):
        branches = {}
        with cwd(path):
            _system([git, 'pack-refs', '--all'])
            if os.path.exists('.git/packed-refs'):
                # It seems the file does not exist if the repository is empty
                with open('.git/packed-refs') as fh:
                    for line in fh:
                        if re.search(r'\A#', line):
                            continue
                        m = re.search(r'\A(\S+)\s+refs/remotes/origin/(.*)', line)
                        if m:
                            branches[ m.group(2) ] = m.group(1)
        return branches




    def get_next_build_number(self, server):
        counter_file = os.path.join(server['root'], 'counter.txt')
        if os.path.exists(counter_file):
            with open(counter_file, 'r+') as fh:
                fcntl.lockf(fh, fcntl.LOCK_EX)
                count = int(fh.read())
                count += 1
                fh.seek(0, os.SEEK_SET)
                fh.write(str(count))
        else:
            with open(counter_file, 'w') as fh:
                fcntl.lockf(fh, fcntl.LOCK_EX)
                count = 1
                fh.write(str(count))
        return count

    def clone_repositories(self, server, config, sha1):
        logger = logging.getLogger(__name__)

        logger.debug("Clone the repositories")
        for repo in config['repos']:
            repo_local_name = self.get_repo_local_name(repo)
            code, out = _system([git, 'clone', os.path.join(server['root'], repo_local_name), repo_local_name])
            if code != 0:
                raise Exception("Could not clone repo")
            with cwd(repo_local_name):
                logger.debug("Check out the given shas")
                code, out = _system([git, 'checkout', sha1])
                if code != 0:
                    raise Exception("Could not checkout sha1")

    def build(self, server, config, sha1):
        logger = logging.getLogger(__name__)
        build_number = self.get_next_build_number(server)
        logger.debug("Build number: {}".format(build_number))
        # TODO store the build in some queue and also allow the parallel execution of jobs on agents

        # update_local_repositories()
        build_parent_directory = os.path.join(server['workdir'], str(build_number))
        logger.debug("Build parent dir: {}".format(build_parent_directory))
        os.mkdir(build_parent_directory)

        bg = self.add_build_logger(build_parent_directory)
        logger.debug("Starting Build {} in directory: {}".format(build_number, build_parent_directory))

        results = {'status': 'success'}
        if 'matrix' in config:
            results['matrix'] = {}
            subbuild = 0
            for case in config['matrix']:
                subbuild += 1
                logger.debug("On agent '{}' schedule exe: '{}'".format(case['agent'], case['exe']))
                if case['agent'] not in server['agents']:
                    results['status'] = 'failure'
                    results['matrix'][subbuild] = {
                        'agent': case['agent'],
                        'error': "Agent is not available.",
                    }
                    continue

                if case['agent'] == 'master':
                    # TODO use the limit to run in parallel
                    build_directory = os.path.join( build_parent_directory, str(subbuild) )
                    os.mkdir(build_directory)
                    with cwd(build_directory):
                        self.clone_repositories(server, config, sha1)
                        code, out = _system(case['exe'])
                        results['matrix'][subbuild] = {
                            'exit': code,
                            'agent': case['agent'],
                            'exe': case['exe'],
                            'out': out,
                        }
                        if code != 0:
                            results['status'] = 'failure'
                else:
                    pass
                    results['status'] = 'failure'
                    # TODO
                    # ssh host
                    # scp our code, the configuration files (that are appropriate to that machine)
                    # clone the directories
                    # run the build
        else:
            build_directory = build_parent_directory
            with cwd(build_directory):
                self.clone_repositories(server, config, sha1)
                if 'steps' in config:
                    results['steps'] = []
                    logger.debug("Run the steps defined in the configuration")
                    for step in config['steps']:
                        logger.debug(step)
                        m = re.search(r'\Acli:\s*(.*)', step)
                        cmd = m.group(1)
                        logger.debug(cmd)
                        code, out = _system(cmd)
                        results['steps'].append({
                            'exit': code,
                            'agent': 'master',
                            'out': out,
                            'step': step,
                        })
                        if code != 0:
                            results['status'] = 'failure'
                            break

        with open(os.path.join(build_parent_directory, 'results.json'), "w") as fh:
            #json.dump(results, fh)
            json.dump(results, fh, sort_keys=True, indent=4, separators=(',', ': '), ensure_ascii=False)

        logger.removeHandler(bg)
        return build_number

    def get_repo_local_name(self, repo):
        m = re.search(r'/([^/]*?)(\.git)?\Z', repo['url'])
        if not m:
            raise Exception("Could not parse repo url '{}'".format(repo['url']))
        return m.group(1)

    def update_central_repos(self, config, server):
        logger = logging.getLogger(__name__)

        # TODO: the first time we clone, ssh might want to verify the server an we might need to manually accept it.
        # TODO: How can we automate this?
        # print(config)
        first = True # only return branches of the first repository
        for repo in config['repos']:
            logger.debug("Repo url {}".format(repo['url']))
            if repo['type'] != 'git':
                raise Exception("Unsupported repository {}".format(repo['type']))

            repo_local_name = self.get_repo_local_name(repo)

            logger.debug("Local repo dir {}".format(repo_local_name))
            # TODO have a root directory for each project that is under the server root
            # TODO allow the user to supply a local directory
            local_repo_path = os.path.join(server['root'], repo_local_name)
            logger.debug("Local repo path {}".format(local_repo_path))

            if not os.path.exists(local_repo_path):
                logger.debug("clone repo for the first time")
                if 'credentials' in repo:
                    os.environ['GIT_SSH_COMMAND'] = "ssh -i  " + repo['credentials']
                cmd_list = [git, 'clone', repo['url'], repo_local_name]
                logger.debug(' '.join(cmd_list))
                with cwd(server['root']):
                    code, out = _system(cmd_list)
                # get current sha ?? In which branch?
                if first:
                    old_branches = {}
            else:
                logger.debug("update repository")
                if first:
                    old_branches = self.get_branches(local_repo_path)
                cmd_list = [git, 'pull']
                with cwd(local_repo_path):
                    code, out = _system(cmd_list)
            if first:
                new_branches = self.get_branches(local_repo_path)
                first = False
            #logger.debug(yaml.dump(new_branches))
        return old_branches, new_branches

    def main(self):
        self.setup_logger()
        logger = logging.getLogger(__name__)

        parser = argparse.ArgumentParser()
        parser.add_argument('--server', help="Server config file", required=True)
        parser.add_argument('--config', help="Config file", required=True)
        parser.add_argument('--debug', help="Turn on debugging", action="store_true")
        parser.add_argument('--current', help="Run the current commit of the given branch of the main repository. (No new changes incorporated)")
        parser.add_argument('--branch', help="Update all the repositories and then run this branch.")
        args = parser.parse_args()

        if args.debug:
            self.add_logger()

        logger.debug("debug")

        logger.debug(args.server)
        with open(args.server) as fh:
            server = yaml.load(fh)


        logger.debug(args.config)
        with open(args.config) as fh:
            config = yaml.load(fh)

        if args.current:
            repo = config['repos'][0]
            repo_local_name = self.get_repo_local_name(repo)
            local_repo_path = os.path.join(server['root'], repo_local_name)
            branches = self.get_branches(local_repo_path)

            if args.current in branches:
                logger.debug("Branch {} is being built at sha1 {}.".format(args.current, branches[args.current]))
                self.build(server, config, branches[args.current])
            else:
                branch_names = ', '.join(sorted(branches.keys()))
                raise Exception("Barnch {} could not be found in repo {}. Available branches: {}".format(args.current, repo['name'], branch_names))
            return


        old_branches, new_branches = self.update_central_repos(config, server)

        # For each watched(!) repo get a list of branches and the sha for each branch before and after the update.
        # If each repo can have multiple branches then shall we really build all the combinations or should there be
        # a leading repository
        # I think we need to assume that one of the repositories is under test and the others have fixed branches
        # TODO If sha changed
        # TODO If branch disappeared
        # TODO If new branch appeared

        if args.branch:
            if args.branch not in new_branches:
                raise Exception("Branch {} not available (any more?, yet?)".format(args.branch))

            logger.debug("Branch {} is being built at sha1 {}.".format(args.branch, new_branches[args.branch]))
            self.build(server, config, new_branches[args.branch])
            return

        builds = []
        for branch in sorted(new_branches.keys()):

            if branch in old_branches:
                if old_branches[branch] == new_branches[branch]:
                    pass
                else:
                    logger.debug("Branch {} changed.".format(branch))
                    builds.append( self.build(server, config, new_branches[branch]) )
            else:
                logger.debug("New branch seen: {}".format(branch))
                builds.append( self.build(server, config, new_branches[branch]) )

        # Once all the builds have finished we need to collect the success/failure data
        failures = 0
        logger.debug("Number of builds: {}".format(len(builds)))
        for build_number in builds:
            build_parent_directory = os.path.join(server['workdir'], str(build_number))
            results_file = os.path.join(build_parent_directory, 'results.json')
            logger.debug("results file: {}".format(results_file))
            if not os.path.exists(results_file):
                failures += 1
                continue
            with open(results_file) as fh:
                results = json.load(fh)
            if not 'status' in results:
                failures += 1
                continue
            if results['status'] != 'success':
                failures += 1
        logger.debug("Number of failures: {}".format(failures))
        exit(failures)

if __name__ == '__main__':
    CI().main()


