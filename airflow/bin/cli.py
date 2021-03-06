#!/usr/bin/env python

import airflow
from airflow.configuration import conf
from airflow.executors import DEFAULT_EXECUTOR
from airflow import settings
from airflow import utils
from airflow import jobs
from airflow.models import DagBag, TaskInstance, DagPickle

import dateutil.parser
from datetime import datetime
import logging
import os
import subprocess
import sys

import argparse

DAGS_FOLDER = os.path.expanduser(conf.get('core', 'DAGS_FOLDER'))

# Common help text across subcommands
mark_success_help = "Mark jobs as succeeded without running them"
subdir_help = "File location or directory from which to look for the dag"


def log_to_stdout():
    log = logging.getLogger()
    log.setLevel(logging.DEBUG)
    logformat = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logformat)
    log.addHandler(ch)


def backfill(args):
    logging.basicConfig(level=logging.INFO, format=settings.SIMPLE_LOG_FORMAT)
    dagbag = DagBag(args.subdir)
    if args.dag_id not in dagbag.dags:
        raise Exception('dag_id could not be found')
    dag = dagbag.dags[args.dag_id]

    if args.start_date:
        args.start_date = dateutil.parser.parse(args.start_date)
    if args.end_date:
        args.end_date = dateutil.parser.parse(args.end_date)

    # If only one date is passed, using same as start and end
    args.end_date = args.end_date or args.start_date
    args.start_date = args.start_date or args.end_date

    if args.task_regex:
        dag = dag.sub_dag(
            task_regex=args.task_regex,
            include_upstream=not args.ignore_dependencies)
    dag.run(
        start_date=args.start_date,
        end_date=args.end_date,
        mark_success=args.mark_success,
        include_adhoc=args.include_adhoc,
        local=args.local)


def run(args):

    utils.pessimistic_connection_handling()
    # Setting up logging
    log = os.path.expanduser(conf.get('core', 'BASE_LOG_FOLDER'))
    directory = log + "/{args.dag_id}/{args.task_id}".format(args=args)
    if not os.path.exists(directory):
        os.makedirs(directory)
    args.execution_date = dateutil.parser.parse(args.execution_date)
    iso = args.execution_date.isoformat()
    filename = "{directory}/{iso}".format(**locals())
    logging.basicConfig(
        filename=filename,
        level=logging.INFO,
        format=settings.LOG_FORMAT)
    if not args.pickle:
        dagbag = DagBag(args.subdir)
        if args.dag_id not in dagbag.dags:
            msg = 'DAG [{0}] could not be found'.format(args.dag_id)
            logging.error(msg)
            raise Exception(msg)
        dag = dagbag.dags[args.dag_id]
        task = dag.get_task(task_id=args.task_id)
    else:
        session = settings.Session()
        logging.info('Loading pickle id {args.pickle}'.format(**locals()))
        dag_pickle = session.query(
            DagPickle).filter(DagPickle.id == args.pickle).first()
        if not dag_pickle:
            raise Exception("Who hid the pickle!? [missing pickle]")
        dag = dag_pickle.pickle
        task = dag.get_task(task_id=args.task_id)

    ti = TaskInstance(task, args.execution_date)

    if args.local:
        print("Logging into: " + filename)
        run_job = jobs.LocalTaskJob(
            task_instance=ti,
            mark_success=args.mark_success,
            force=args.force,
            pickle_id=args.pickle,
            ignore_dependencies=args.ignore_dependencies)
        run_job.run()
    elif args.raw:
        ti.run(
            mark_success=args.mark_success,
            force=args.force,
            ignore_dependencies=args.ignore_dependencies,
            job_id=args.job_id,
        )
    else:
        try:
            # Running remotely, so pickling the DAG
            session = settings.Session()
            pickle = DagPickle(dag)
            session.add(pickle)
            session.commit()
            pickle_id = pickle.id
            print(
                'Pickled dag {dag} '
                'as pickle_id:{pickle_id}').format(**locals())
        except Exception as e:
            print('Could not pickle the DAG')
            print(e)
            pickle_id = None

        executor = DEFAULT_EXECUTOR
        executor.start()
        cmd = ti.command(
            force=args.force,
            local=True,
            mark_success=args.mark_success,
            ignore_dependencies=args.ignore_dependencies,
            pickle_id=pickle_id)
        print("Sending run command to executor:\n" + cmd)
        executor.queue_command(ti.key, cmd)
        executor.end()


def list_dags(args):
    dagbag = DagBag()
    print("\n".join(sorted(dagbag.dags)))


def list_tasks(args):
    dagbag = DagBag()
    if args.dag_id not in dagbag.dags:
        raise Exception('dag_id could not be found')
    dag = dagbag.dags[args.dag_id]
    if args.tree:
        dag.tree_view()
    else:
        tasks = sorted([t.task_id for t in dag.tasks])
        print("\n".join(sorted(tasks)))


def test(args):
    log_to_stdout()
    args.execution_date = dateutil.parser.parse(args.execution_date)
    dagbag = DagBag(args.subdir)
    if args.dag_id not in dagbag.dags:
        raise Exception('dag_id could not be found')
    dag = dagbag.dags[args.dag_id]
    task = dag.get_task(task_id=args.task_id)
    ti = TaskInstance(task, args.execution_date)
    ti.run(force=True, ignore_dependencies=True, test_mode=True)


def clear(args):
    logging.basicConfig(level=logging.INFO, format=settings.SIMPLE_LOG_FORMAT)
    dagbag = DagBag(args.subdir)

    if args.dag_id not in dagbag.dags:
        raise Exception('dag_id could not be found')
    dag = dagbag.dags[args.dag_id]

    if args.start_date:
        args.start_date = datetime.strptime(args.start_date, '%Y-%m-%d')
    if args.end_date:
        args.end_date = datetime.strptime(args.end_date, '%Y-%m-%d')

    if args.task_regex:
        dag = dag.sub_dag(
            task_regex=args.task_regex,
            include_downstream=args.downstream,
            include_upstream=args.upstream,
        )
    dag.clear(
        start_date=args.start_date,
        end_date=args.end_date,
        only_failed=args.only_failed,
        only_running=args.only_running,
        confirm_prompt=True)


def webserver(args):
    print(settings.HEADER)
    log_to_stdout()
    from airflow.www.app import app
    if args.debug:
        print(
            "Starting the web server on port {0} and host {1}.".format(
                args.port, args.hostname))
        app.run(debug=True, port=args.port, host=args.hostname)
    else:
        print(
            'Running Tornado server on host {host} and port {port}...'.format(
                host=args.hostname, port=args.port))
        from tornado.httpserver import HTTPServer
        from tornado.ioloop import IOLoop
        from tornado.wsgi import WSGIContainer

        # simple multi-process server
        server = HTTPServer(WSGIContainer(app))
        server.bind(args.port)
        server.start(0)  # Forks multiple sub-processes
        IOLoop.instance().start()


def master(args):
    print(settings.HEADER)
    log_to_stdout()
    job = jobs.MasterJob(args.dag_id, args.subdir)
    job.run()


def serve_logs(args):
    print("Starting flask")
    import flask
    flask_app = flask.Flask(__name__)

    @flask_app.route('/log/<path:filename>')
    def serve_logs(filename):
        log = os.path.expanduser(conf.get('core', 'BASE_LOG_FOLDER'))
        return flask.send_from_directory(
            log,
            filename,
            mimetype="application/json",
            as_attachment=False)
    WORKER_LOG_SERVER_PORT = \
        int(conf.get('celery', 'WORKER_LOG_SERVER_PORT'))
    flask_app.run(
        host='0.0.0.0', port=WORKER_LOG_SERVER_PORT)


def worker(args):
    # Worker to serve static log files through this simple flask app
    sp = subprocess.Popen(['airflow', 'serve_logs'])

    # Celery worker
    from airflow.executors.celery_executor import app as celery_app
    from celery.bin import worker

    worker = worker.worker(app=celery_app)
    options = {
        'optimization': 'fair',
        'O': 'fair',
    }
    worker.run(**options)
    sp.kill()


def initdb(args):
    print("DB: " + conf.get('core', 'SQL_ALCHEMY_CONN'))
    utils.initdb()
    print("Done.")


def resetdb(args):
    print("DB: " + conf.get('core', 'SQL_ALCHEMY_CONN'))
    if raw_input(
            "This will drop exisiting tables if they exist. "
            "Proceed? (y/n)").upper() == "Y":
        logging.basicConfig(level=logging.DEBUG,
                            format=settings.SIMPLE_LOG_FORMAT)
        utils.resetdb()
    else:
        print("Bail.")


def version(args):
    print(settings.HEADER + "  v" + airflow.__version__)


def flower(args):
    broka = conf.get('celery', 'BROKER_URL')
    port = '--port=' + args.port
    api = ''
    if args.broker_api:
        api = '--broker_api=' + args.broker_api
    sp = subprocess.Popen(['flower', '-b', broka, port, api])
    sp.wait()


def get_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help='sub-command help')

    ht = "Run subsections of a DAG for a specified date range"
    parser_backfill = subparsers.add_parser('backfill', help=ht)
    parser_backfill.add_argument("dag_id", help="The id of the dag to run")
    parser_backfill.add_argument(
        "-t", "--task_regex",
        help="The regex to filter specific task_ids to backfill (optional)")
    parser_backfill.add_argument(
        "-s", "--start_date", help="Override start_date YYYY-MM-DD")
    parser_backfill.add_argument(
        "-e", "--end_date", help="Override end_date YYYY-MM-DD")
    parser_backfill.add_argument(
        "-m", "--mark_success",
        help=mark_success_help, action="store_true")
    parser_backfill.add_argument(
        "-l", "--local",
        help=mark_success_help, action="store_true")
    parser_backfill.add_argument(
        "-a", "--include_adhoc",
        help=mark_success_help, action="store_true")
    parser_backfill.add_argument(
        "-i", "--ignore_dependencies",
        help=(
            "Skip upstream tasks, run only the tasks "
            "matching the regexp. Only works in conjunction with task_regex"),
        action="store_true")
    parser_backfill.add_argument(
        "-sd", "--subdir", help=subdir_help,
        default=DAGS_FOLDER)
    parser_backfill.set_defaults(func=backfill)

    ht = "Clear a set of task instance, as if they never ran"
    parser_clear = subparsers.add_parser('clear', help=ht)
    parser_clear.add_argument("dag_id", help="The id of the dag to run")
    parser_clear.add_argument(
        "-t", "--task_regex",
        help="The regex to filter specific task_ids to clear (optional)")
    parser_clear.add_argument(
        "-s", "--start_date", help="Overide start_date YYYY-MM-DD")
    parser_clear.add_argument(
        "-e", "--end_date", help="Overide end_date YYYY-MM-DD")
    ht = "Include upstream tasks"
    parser_clear.add_argument(
        "-u", "--upstream", help=ht, action="store_true")
    ht = "Only failed jobs"
    parser_clear.add_argument(
        "-f", "--only_failed", help=ht, action="store_true")
    ht = "Only running jobs"
    parser_clear.add_argument(
        "-r", "--only_running", help=ht, action="store_true")
    ht = "Include downstream tasks"
    parser_clear.add_argument(
        "-d", "--downstream", help=ht, action="store_true")
    parser_clear.add_argument(
        "-sd", "--subdir", help=subdir_help,
        default=DAGS_FOLDER)
    parser_clear.set_defaults(func=clear)

    ht = "Run a single task instance"
    parser_run = subparsers.add_parser('run', help=ht)
    parser_run.add_argument("dag_id", help="The id of the dag to run")
    parser_run.add_argument("task_id", help="The task_id to run")
    parser_run.add_argument(
        "execution_date", help="The execution date to run")
    parser_run.add_argument(
        "-sd", "--subdir", help=subdir_help,
        default=DAGS_FOLDER)
    parser_run.add_argument(
        "-m", "--mark_success", help=mark_success_help, action="store_true")
    parser_run.add_argument(
        "-f", "--force",
        help="Force a run regardless or previous success",
        action="store_true")
    parser_run.add_argument(
        "-l", "--local",
        help="Runs the task locally, don't use the executor",
        action="store_true")
    parser_run.add_argument(
        "-r", "--raw",
        help=argparse.SUPPRESS,
        action="store_true")
    parser_run.add_argument(
        "-i", "--ignore_dependencies",
        help="Ignore upstream and depends_on_past dependencies",
        action="store_true")
    parser_run.add_argument(
        "-p", "--pickle",
        help="Serialized pickle object of the entire dag (used internally)")
    parser_run.add_argument(
        "-j", "--job_id", help=argparse.SUPPRESS)
    parser_run.set_defaults(func=run)

    ht = (
        "Test a task instance. This will run a task without checking for "
        "dependencies or recording it's state in the database."
    )
    parser_test = subparsers.add_parser('test', help=ht)
    parser_test.add_argument("dag_id", help="The id of the dag to run")
    parser_test.add_argument("task_id", help="The task_id to run")
    parser_test.add_argument(
        "execution_date", help="The execution date to run")
    parser_test.add_argument(
        "-sd", "--subdir", help=subdir_help,
        default=DAGS_FOLDER)
    parser_test.set_defaults(func=test)

    ht = "Start a Airflow webserver instance"
    parser_webserver = subparsers.add_parser('webserver', help=ht)
    parser_webserver.add_argument(
        "-p", "--port",
        default=conf.get('webserver', 'WEB_SERVER_PORT'),
        type=int,
        help="Set the port on which to run the web server")
    parser_webserver.add_argument(
        "-hn", "--hostname",
        default=conf.get('webserver', 'WEB_SERVER_HOST'),
        help="Set the hostname on which to run the web server")
    ht = "Use the server that ships with Flask in debug mode"
    parser_webserver.add_argument(
        "-d", "--debug", help=ht, action="store_true")
    parser_webserver.set_defaults(func=webserver)

    ht = "Start a master scheduler instance"
    parser_master = subparsers.add_parser('master', help=ht)
    parser_master.add_argument(
        "-d", "--dag_id", help="The id of the dag to run")
    parser_master.add_argument(
        "-sd", "--subdir", help=subdir_help,
        default=DAGS_FOLDER)
    parser_master.set_defaults(func=master)

    ht = "Initialize the metadata database"
    parser_initdb = subparsers.add_parser('initdb', help=ht)
    parser_initdb.set_defaults(func=initdb)

    ht = "Burn down and rebuild the metadata database"
    parser_resetdb = subparsers.add_parser('resetdb', help=ht)
    parser_resetdb.set_defaults(func=resetdb)

    ht = "List the DAGs"
    parser_list_dags = subparsers.add_parser('list_dags', help=ht)
    parser_list_dags.set_defaults(func=list_dags)

    ht = "List the tasks whithin a DAG"
    parser_list_tasks = subparsers.add_parser('list_tasks', help=ht)
    parser_list_tasks.add_argument(
        "-t", "--tree", help="Tree view", action="store_true")
    parser_list_tasks.add_argument(
        "dag_id", help="The id of the dag")
    parser_list_tasks.set_defaults(func=list_tasks)

    ht = "Start a Celery worker node"
    parser_worker = subparsers.add_parser('worker', help=ht)
    parser_worker.set_defaults(func=worker)

    ht = "Serve logs generate by worker"
    parser_logs = subparsers.add_parser('serve_logs', help=ht)
    parser_logs.set_defaults(func=serve_logs)

    ht = "Start a Celery Flower"
    parser_flower = subparsers.add_parser('flower', help=ht)
    parser_flower.add_argument(
        "-p", "--port", help="The port",
        default='5555')
    parser_flower.add_argument(
        "-a", "--broker_api", help="Broker api")
    parser_flower.set_defaults(func=flower)

    parser_version = subparsers.add_parser('version', help="Show version")
    parser_version.set_defaults(func=version)

    return parser
