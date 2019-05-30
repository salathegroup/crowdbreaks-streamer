from flask import Flask, request, Blueprint, Response, jsonify
from flask import current_app as app
from app.basic_auth import requires_auth_func
import json
from app.extensions import es, redis
import logging
from app.utils.priority_queue import TweetIdQueue
from app.utils.predict_sentiment import PredictSentiment
from app.stream.tasks import predict, handle_tweet
import time
from statsmodels.nonparametric.smoothers_lowess import lowess
import numpy as np
import os
from helpers import report_error
from app.utils.mailer import StreamStatusMailer, Mailer
from app.utils.priority_queue import TweetIdQueue


blueprint = Blueprint('main', __name__)
logger = logging.getLogger('Main')


@blueprint.before_request
def require_auth_all():
    return requires_auth_func()

@blueprint.route('/', methods=['GET'])
def index():
    return "hello world!!!"


#################################################################
# TESTS
@blueprint.route('test/redis', methods=['GET'])
def test_redis():
    return json.dumps(redis.test_connection())

@blueprint.route('test/celery', methods=['GET'])
def test_celery():
    with open(os.path.join(app.config['CONFIG_PATH'], 'example_data', 'tweet.json'), 'r') as f:
        tweet = json.load(f)
    handle_tweet(tweet, send_to_es=False, use_pq=False, debug=True)
    return 'testing celery'

@blueprint.route('test/rollbar', methods=['GET'])
def test_rollbar():
    report_error(logger, 'test error')
    return 'error reported'

@blueprint.route('test/pq', methods=['GET'])
def test_pq():
    tid = TweetIdQueue('project_vaccine_sentiment', priority_threshold=3)
    return tid.pq.list()

@blueprint.route('test/email/ping', methods=['GET'])
def test_email_ping():
    mailer = Mailer()
    return mailer.client.users.ping()

@blueprint.route('test/email/send_test_email', methods=['GET'])
def test_send_email():
    status_type = request.args.get('type', default='daily', type='str')
    mailer = StreamStatusMailer(status_type=status_type)
    body = mailer.get_body()
    resp = mailer.send_status(body)
    return json.dumps(resp)

@blueprint.route('test/email/status', methods=['GET'])
def test_email_status():
    status_type = request.args.get('type', default='daily', type=str)
    mailer = StreamStatusMailer(status_type=status_type)
    body = mailer.get_body()
    return body

#################################################################
# TWEET ID HANDLING
@blueprint.route('tweet/new/<project>', methods=['GET'])
def get_new_tweet(project):
    """"Get new tweet from priority queue"""
    user_id = request.args.get('user_id', None)
    fields = request.args.get('fields', ['id', 'text'])
    tid = TweetIdQueue(project)
    tweet = tid.get_tweet(user_id=user_id)
    if tweet is None:
        msg = 'Could not get tweet id from priority queue. Getting random tweet from ES instead.'
        report_error(logger, msg)
        # get a random tweet instead
        tweet = es.get_random_document(project)
        if tweet is None:
            msg = 'Could not get random tweet from elasticsearch.'
            report_error(logger, msg)
            return jsonify({'error': msg}), 400
    tweet = {k: tweet.get(k) for k in fields}
    if 'id' in tweet:
        # rename fields
        tweet['tweet_id'] = str(tweet.pop('id'))
        tweet['tweet_text'] = tweet.pop('text')
    return jsonify(tweet)

@blueprint.route('tweet/update/<project>', methods=['POST'])
def add_to_pq(project):
    """Update priority score in queue and remember that a user has already classified a tweet"""
    data = request.get_json()
    logger.debug('Incoming request with data {}'.format(data))
    if data is None or 'user_id' not in data or 'tweet_id' not in data:
        report_error(logger, 'No user_id was passed when updating ')
        return Response(None, status=400, mimetype='text/plain')
    tid = TweetIdQueue(project)
    tid.update(data['tweet_id'], data['user_id'])
    return Response('Update successful.', status=200, mimetype='text/plain')


@blueprint.route('tweet/remove/<project>', methods=['POST'])
def remove_from_pq(project):
    """Remove a tweet which is now private"""
    data = request.get_json()
    logger.debug('Incoming request with data {}'.format(data))
    if data is None or 'tweet_id' not in data:
        report_error(logger, 'No tweet_id was passed when updating')
        return Response(None, status=400, mimetype='text/plain')
    tid = TweetIdQueue(project)
    tid.remove(data['tweet_id'])
    return Response('Successfully removed.', status=200, mimetype='text/plain')

#################################################################
# All data
@blueprint.route('data/all/<index_name>', methods=['GET'])
def get_all_data(index_name):
    options = get_params(request.args)
    res = es.get_all_agg(index_name, **options)
    return json.dumps(res)

#################################################################
# Sentiment data
@blueprint.route('sentiment/vaccine/', methods=['POST', 'GET'])
def get_vaccine_sentiment(model='fasttext_v1.ftz'):
    text = None
    if request.method == 'POST':
        data = request.get_json()
        logger.debug('Incoming request with data {}'.format(data))
        text = data.get('text', None)
        if text is None:
            return Response(None, status=400, mimetype='text/plain')
    else:
        text = 'This is just a test string'
    ps = PredictSentiment()
    prediction = ps.predict(text, model=model)
    return json.dumps(prediction)

@blueprint.route('sentiment/data/<value>', methods=['GET'])
def get_vaccine_data(value):
    options = get_params(request.args)
    res = es.get_sentiment_data('project_vaccine_sentiment', value, **options)
    for d in res:
        if d['doc_count'] == 0:
            d['doc_count'] = 'null'
    return json.dumps(res)

@blueprint.route('sentiment/average', methods=['GET'])
def get_average_sentiment():
    options = get_params(request.args)
    res = es.get_av_sentiment('project_vaccine_sentiment', **options)
    res_lowess = compute_loess(res)
    return json.dumps(res_lowess)

@blueprint.route('sentiment/geo', methods=['GET'])
def get_geo_sentiment():
    options = get_params(request.args)
    res = es.get_geo_sentiment('project_vaccine_sentiment', **options)
    return json.dumps(res)

def get_params(args):
    options = {}
    # dates must be of format 'yyyy-MM-dd HH:mm:ss' or 'now-*'
    options['interval'] = args.get('interval', 'month')
    options['start_date'] = args.get('start_date', 'now-20y')
    options['end_date'] = args.get('end_date', 'now')
    options['include_retweets'] = args.get('include_retweets', False)
    if isinstance(options['include_retweets'], str):
        options['include_retweets'] = True if options['include_retweets'] == 'true' else False
    return options

def compute_loess(data):
    y = np.array([d['avg_sentiment']['value'] for d in data])
    x = np.array([d['key'] for d in data])
    lowess_fit = lowess(y, x, frac=0.1, is_sorted=True, return_sorted=False)
    data_new = []
    for i, d in enumerate(data):
        if np.isnan(lowess_fit[i]):
            # needed for Ruby to be able to parse NaN values
            d['avg_sentiment']['value_smoothed'] = 'null'
        else:
            d['avg_sentiment']['value_smoothed'] = lowess_fit[i]
        data_new.append(d)
    return data_new
