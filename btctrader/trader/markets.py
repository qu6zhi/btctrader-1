import base64
import hashlib
import hmac
import time
from django.utils import timezone
import urllib
import requests
import models
from trader_settings import market_settings


settings = market_settings()


# Note that all public API functions are expected to return up to 3 outputs:
#   success - A True/False value indicating whether the function succeeded
#   err - If success=False, this field should be populated with an error message
#   result - If success=True, this field should be populated with the actual result of the function
# This is to avoid messy/expensive exception handling and call stack unwinds
# Please ALWAYS verify the value of success when calling an api function
# For convention, all functions implementing this return interface are prefixed with api_
class MarketBase(object):
    """
    Defines the interface for a Market API
    """

    # Not all markets support all currencies, and combinations thereof
    # You should define which currency pairings your market supports
    supported_currency_pairs = (
        ('BTC', 'USD'),
    )

    def __init__(self, market):
        """
        Instantiate the Market API object. Stores a pointer back to the Market
        database object
        """
        self.market = market

    def api_execute_order(self, order):
        """
        Attempt to execute the specified order
        @type order: models.Order
        """
        return False, 'Not implemented', None

    def api_cancel_order(self, order):
        """
        Attempt to cancel the specified order
        @type order: models.Order
        """
        return False, 'Not implemented', None

    def api_update_order_status(self, order):
        """
        Update a single order object with the latest status from the market
        @type order: models.Order
        """
        return False, 'Not implemented', None

    def api_update_market(self):
        """
        Used to update the state of the market as a whole. It's suggested that
        this be used to update both current market pricing and depth information,
        as well as all currently open orders. This is intended to be called with
        relatively low frequency, typically once per minute
        """
        return False, 'Not implemented', None

    def api_get_total_amount_after_fees(self, amount, order_type, currency):
        """
        Calculate the final amount, after fees have been subtracted
        Based on both a currency, and an order type (Buy/Sell)
        @type currency: models.Currency
        """
        return False, 'Not implemented', None

    def api_get_total_amount_incl_fees(self, amount, order_type, currency):
        """
        Calculate a total amount required for a trade, such that the target
        amount is a certain value, once fees have been subtracted (i.e. this
        gives the total amount INCLUDING fees)
        Based on both a currency, and an order type (Buy/Sell)
        @type currency: models.Currency
        """
        return False, 'Not implemented', None

    def api_get_current_market_price(self, force_update=False, currency_from=None, currency_to=None):
        """
        Returns a current MarketPrice object for the market. Markets can choose
        to introduce caching here if desired (i.e. only update with live data
        once every X seconds). It's also optional to actually save the market
        price to the database (although recommended if caching). If force_update
        is true, then caching behavior should be bypassed
        @type currency_from: models.Currency
        @type currency_to: models.Currency
        """
        return False, 'Not implemented', None


# Be VERY careful - should NOT be changed unless no longer correct
MTGOX_CURRENCY_DIVISIONS = {
    'BTC': 100000000,
    'USD': 100000,
    'GBP': 100000,
    'EUR': 100000,
    'JPY': 1000,
    'AUD': 100000,
    'CAD': 100000,
    'CHF': 100000,
    'CNY': 100000,
    'DKK': 100000,
    'HKD': 100000,
    'PLN': 100000,
    'RUB': 100000,
    'SEK': 1000,
    'SGD': 100000,
    'THB': 100000,
}

# Constant value enforced in the API
MTGOX_MINIMUM_TRADE_BTC = 0.01

MTGOX_API_BASE_URL = 'https://data.mtgox.com/api/2/'


class MtGoxMarket(MarketBase):
    """
    Market interface for MtGox
    https://www.mtgox.com/

    Utilizes the (frustratingly incomplete) version 2 API. Implemented based on the unofficial documentation here:
    https://bitbucket.org/nitrous/mtgox-api/overview

    API keys are obtained from here (requires MtGox account):
    https://mtgox.com/security
    """

    supported_currency_pairs = (
        ('BTC', 'USD'),
        ('BTC', 'GBP'),
        ('BTC', 'EUR'),
        ('BTC', 'JPY'),
        ('BTC', 'AUD'),
        ('BTC', 'CAD'),
        ('BTC', 'CHF'),
        ('BTC', 'CNY'),
        ('BTC', 'DKK'),
        ('BTC', 'HKD'),
        ('BTC', 'PLN'),
        ('BTC', 'RUB'),
        ('BTC', 'SEK'),
        ('BTC', 'SGD'),
        ('BTC', 'THB'),
    )

    timeout = 15
    tryout = 5

    def __init__(self, market):
        super(MtGoxMarket, self).__init__(market)

        # Change these to reflect your actual API keys
        self.api_key = settings.mtgox_api_key
        self.api_secret = settings.mtgox_api_secret

        # Internal storage for the current trading fee
        # Varies from account to account - must be updated via the API
        self.trade_fee = 0
        self.trade_fee_valid = False

        # Rolling window to limit requests made to the API
        self.reqs = {'max': 10, 'window': 10}
        self.req_timestamps = []

        # Default currency pair; used for API calls where the currency makes no difference
        self.default_currency_pair = self.market.default_currency_from.abbrev + self.market.default_currency_to.abbrev

        # Caching rules for market price data
        # If the last price is older than this number of seconds, an API call will be made to refresh the price
        self.market_price_max_age = 60

    def throttle(self):
        # Make sure we don't send more than a given number of requests in a certain
        # time window

        # First clear out old request timestamps
        current_timestamp = timezone.now()
        for timestamp in self.req_timestamps:
            if (current_timestamp - timestamp).total_seconds() > self.reqs['window']:
                self.req_timestamps.remove(timestamp)
            else:
                break

        # Now add the current timestamp
        self.req_timestamps.append(current_timestamp)

        # Now see if we have too many requests
        if len(self.req_timestamps) > self.reqs['max']:
            time.sleep(self.reqs['window'] - (current_timestamp - self.req_timestamps[0]).total_seconds())

    def nonce(self):
        return str(int(time.time() * 1000))

    def api_request(self, path, post_data=None, check_success=True, authenticate=True, post=True):
        # Convert input to a list if we got a dict
        if post_data is not None:
            if isinstance(post_data, dict):
                post_data = post_data.items()
        else:
            post_data = []

        # Add the nonce
        if authenticate:
            post_data.insert(0, ('nonce', self.nonce()))

        # Build the POST data
        post_data_str = urllib.urlencode(post_data)

        # Build the encryption
        headers = {}
        if authenticate:
            hash_data = path + chr(0) + post_data_str
            signature = base64.b64encode(str(hmac.new(
                base64.b64decode(self.api_secret),
                hash_data,
                hashlib.sha512
            ).digest()))

            # Build the headers
            headers = {
                'User-Agent': 'btctrader',
                'Rest-Key': self.api_key,
                'Rest-Sign': signature,
                'Accept-Encoding': 'gzip',
                'Content-Type': 'application/x-www-form-urlencoded',
            }
        else:
            headers = {
                'User-Agent': 'btctrader'
            }

        tries = 0
        resp = None
        while tries < self.tryout:
            tries += 1

            # We want a hard throttle on requests to avoid being blocked
            self.throttle()

            # Make the actual request
            try:
                if post:
                    resp = requests.post(MTGOX_API_BASE_URL + path,
                                         data=post_data_str,
                                         headers=headers,
                                         timeout=self.timeout)
                else:
                    resp = requests.get(MTGOX_API_BASE_URL + path,
                                         data=post_data_str,
                                         headers=headers,
                                         timeout=self.timeout)
            except requests.Timeout:
                continue

            # If we got to here, the request did not timeout
            break

        # Check for failure response
        if resp is None or resp.status_code != 200:
            return False,\
                'HTTP request failed: API returned status %s (request path %s)' % (resp.status_code, path),\
                resp

        resp_json = resp.json()
        if check_success:
            if resp_json['result'] != 'success':
                return False, 'API request did not return success response (request path %s)' % path, resp_json
            else:
                return True, None, resp_json['data']
        else:
            return True, None, resp_json

    def api_get_info(self):
        return self.api_request(path=self.default_currency_pair + '/money/info')

    def api_get_trade_fee(self):
        if not self.trade_fee_valid:
            success, err, info = self.api_get_info()
            if not success:
                return success, err, info

            self.trade_fee = float(info['Trade_Fee'])
            self.trade_fee_valid = True

        return True, None, self.trade_fee

    def api_execute_order(self, order):
        # Should we even be executing this order?
        if order.amount < MTGOX_MINIMUM_TRADE_BTC:
            return False, 'Trade amount lower than MtGox minimum trade', None
        if order.status != 'N' or order.market_order_id != '':
            return False, 'Order has already been submitted to MtGox', None
        if not (order.currency_from.abbrev, order.currency_to.abbrev) in self.supported_currency_pairs:
            return False, 'MtGox does not support this currency pairing', None

        # Get the current trade fee associated with this account
        # success, err, fee = self.api_get_trade_fee()
        # if not success:
        #     return success, err, fee

        # Build the trade request
        trade_req = {}

        # type
        if order.order_type == 'B':
            trade_req['type'] = 'bid'
        elif order.order_type == 'S':
            trade_req['type'] = 'ask'
        else:
            return False, 'Unsupported order type: %s' % order.order_type, None

        # amount_int
        # Make sure to multiply by the division factor and convert to an int
        # TODO: Account for the trade fee here? Or somewhere else?
        trade_req['amount_int'] = int(order.amount * MTGOX_CURRENCY_DIVISIONS[order.currency_from.abbrev])

        # price_int
        if not order.market_order:
            if order.price > 0:
                trade_req['price_int'] = int(order.price * MTGOX_CURRENCY_DIVISIONS[order.currency_to.abbrev])
            else:
                return False, 'Must specify a price for a non-market order', None

        # Send the trade request
        success, err, result = self.api_request(path=order.get_currency_pair() + '/money/order/add',
                                                post_data=trade_req)
        if not success:
            return success, err, result

        # Save the order ID and update the status
        order.market_order_id = str(result)
        order.status = 'O'
        order.save()

        # Invalidate trade fee
        self.trade_fee_valid = False

        return True, None, None

    def api_cancel_order(self, order):
        # Can we even cancel this order?
        if order.status not in ('O', 'E'):
            return False, 'Order is not currently open or executing - cannot cancel', None

        # Attempt to cancel
        success, err, result = self.api_request(path=order.get_currency_pair() + '/money/order/cancel',
                                                post_data={'oid': order.market_order_id},
                                                check_success=False)
        if not success:
            return success, err, result

        if result['result'] != 'success':
            return False, 'Unable to cancel order', None
        else:
            return True, None, None

    def update_db_order_status(self, db_order, mtgox_orders):
        found = False
        for open_order in mtgox_orders:
            if open_order['oid'] == db_order.market_order_id:
                # Validate order parameters - if MtGox doesn't agree with the database then there's a serious problem
                if open_order['currency'] != db_order.currency_to.abbrev:
                    return False, 'Order currency_to does not match expected value (expected %s, got %s)' %\
                                  (db_order.currency_to.abbrev, open_order['currency']), None
                if open_order['item'] != db_order.currency_from.abbrev:
                    return False, 'Order currency_from does not match expected value (expected %s, got %s)' %\
                                  (db_order.currency_from.abbrev, open_order['item']), None
                if open_order['amount'] != db_order.amount:
                    return False, 'Order amount does not match expected value (expected %s, got %s)' %\
                                  (db_order.amount, open_order['amount']), None
                if db_order.market_order and float(open_order['price']) != 0:
                    return False, 'Order expected to be a market order, got price %s' % open_order['price'], None

                # Update status
                if open_order['status'] in ['pending', 'executing', 'post-pending']:
                    db_order.status = 'E'
                elif open_order['status'] == 'open':
                    db_order.status = 'O'
                elif open_order['status'] == 'invalid':
                    db_order.status = 'I'
                else:
                    db_order.status = 'U'

                found = True
                break

        # TODO: Could it have been cancelled? No way to tell in current API version!
        # /money/orders does not return filled orders - if it wasn't found, assume it was filled
        # For now, only update to Filled if the order was Open or Executing
        if not found and db_order.status in ['O', 'E']:
            db_order.status = 'F'

        db_order.save()

        return True, None, None

    def api_update_order_status(self, order):
        # Currently the v2 API call for info on a specific order is broke
        # Hence retrieve info for all orders and filter from there
        success, err, mtgox_orders = self.api_request(path=order.get_currency_pair() + '/money/orders')
        if not success:
            return success, err, mtgox_orders

        return self.update_db_order_status(order, mtgox_orders)

    def api_update_market(self):
        # Update all orders

        # TODO: Do we need to split this request into separate requests for different currency pairs, per order?
        # It's possible that the API returns all orders no matter which currency you specify - need to test
        success, err, mtgox_orders = self.api_request(path=self.default_currency_pair + '/money/orders')
        if not success:
            return success, err, mtgox_orders

        db_orders = self.market.order_set.filter(order_status__in=['N', 'O', 'E', 'U'])
        for db_order in db_orders:
            success, err, result = self.update_db_order_status(db_order, mtgox_orders)
            if not success:
                return success, err, result

        # Update market price
        success, err, market_price = self.api_get_current_market_price(self.market)
        if not success:
            return success, err, market_price

        return True, None, None

    def api_get_current_market_price(self, force_update=False, currency_from=None, currency_to=None):
        currency_pair = self.default_currency_pair

        # Wrangle the inputs - if we got currencies then use them, otherwise
        # set them to default values
        if currency_from is not None and currency_to is not None:
            currency_pair = currency_from.abbrev + currency_to.abbrev
        else:
            currency_from = self.market.default_currency_from
            currency_to = self.market.default_currency_to

        if (currency_from.abbrev, currency_to.abbrev) not in self.supported_currency_pairs:
            return False, 'Currency pair not supported: %s' % currency_pair, None

        # See if there's a recent price
        if not force_update:
            try:
                last_price = models.MarketPrice.objects.filter(
                    market=self.market,
                    currency_from=currency_from,
                    currency_to=currency_to
                ).order_by('-time')[0]

                # Is this price recent enough? If so, just return it
                if (timezone.now() - last_price.time).total_seconds() <= self.market_price_max_age:
                    return True, None, last_price

            except IndexError:
                # Don't do anything - this just means we couldn't find any MarketPrice
                # objects for the market/currency
                pass

        # If we got to this point, then we need to make an API call to get the latest price
        success, err, ticker = self.api_request(path=currency_pair + '/money/ticker_fast', authenticate=False,
                                                post=False)
        if not success:
            return success, err, ticker

        # Build the MarketPrice object
        market_price = models.MarketPrice()
        market_price.market = self.market
        market_price.currency_from = currency_from
        market_price.currency_to = currency_to

        # Since we're on the opposite side of the transaction, the lowest "ask" price is
        # what we will be buying for, and vice versa
        market_price.buy_price = float(ticker['sell']['value_int']) / MTGOX_CURRENCY_DIVISIONS[currency_to.abbrev]
        market_price.sell_price = float(ticker['buy']['value_int']) / MTGOX_CURRENCY_DIVISIONS[currency_to.abbrev]

        # Save it so it can be "cached" for next time
        market_price.save()

        return True, None, market_price


# TODO: Check whether this is actually enforced by Bitstamp
BITSTAMP_MINIMUM_TRADE_BTC = 0.01

BITSTAMP_API_BASE_URL = 'https://www.bitstamp.net/api/'


class BitstampMarket(MarketBase):
    """
    Market interface for BitStamp
    https://www.bitstamp.net/

    Implemented based on the official documentation here:
    https://www.bitstamp.net/api/

    No API keys - some functions require authentication using username/password
    """

    # Bitstamp only seems to support BTC/USD for a lot of API functions - although
    # technically the site supports the other commented currencies here too
    supported_currency_pairs = (
        ('BTC', 'USD'),
        #('BTC', 'GBP'),
        #('BTC', 'EUR'),
        #('BTC', 'JPY'),
        #('BTC', 'AUD'),
    )

    timeout = 15
    tryout = 5

    def __init__(self, market):
        super(BitstampMarket, self).__init__(market)

        self.api_user = settings.bitstamp_api_user
        self.api_password = settings.bitstamp_api_password

        # Internal storage for the current trading fee
        # Varies from account to account - must be updated via the API
        self.trade_fee = 0
        self.trade_fee_valid = False

        # Rolling window to limit requests made to the API
        # Max of 600 in 10 minutes
        self.reqs = {'max': 600, 'window': 600}
        self.req_timestamps = []

        # Default currency pair; used for API calls where the currency makes no difference
        self.default_currency_pair = self.market.default_currency_from.abbrev + self.market.default_currency_to.abbrev

        # Caching rules for market price data
        # If the last price is older than this number of seconds, an API call will be made to refresh the price
        self.market_price_max_age = 60

    def throttle(self):
        # Make sure we don't send more than a given number of requests in a certain
        # time window

        # First clear out old request timestamps
        current_timestamp = timezone.now()
        for timestamp in self.req_timestamps:
            if (current_timestamp - timestamp).total_seconds() > self.reqs['window']:
                self.req_timestamps.remove(timestamp)
            else:
                break

        # Now add the current timestamp
        self.req_timestamps.append(current_timestamp)

        # Now see if we have too many requests
        if len(self.req_timestamps) > self.reqs['max']:
            time.sleep(self.reqs['window'] - (current_timestamp - self.req_timestamps[0]).total_seconds())

    def api_request(self, path, post=False, add_credentials=False, data=None):
        # Convert input to a list if we got a dict
        if data is not None:
            if isinstance(data, dict):
                data = data.items()
        else:
            data = []

        if add_credentials:
            data.append(('user', self.api_user))
            data.append(('password', self.api_password))

        # Encode the data
        data_str = urllib.urlencode(data)

        # Build the headers
        headers = {
            'User-Agent': 'btctrader',
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        tries = 0
        resp = None
        while tries < self.tryout:
            tries += 1

            # We want a hard throttle on requests to avoid being blocked
            self.throttle()

            # Make the actual request
            try:
                if post:
                    resp = requests.post(BITSTAMP_API_BASE_URL + path,
                                         data=data_str,
                                         headers=headers,
                                         timeout=self.timeout)
                else:
                    resp = requests.get(BITSTAMP_API_BASE_URL + path,
                                        data=data_str,
                                        headers=headers,
                                        timeout=self.timeout)
            except requests.Timeout:
                continue

            # If we got to here, the request did not timeout
            break

        # Check for failure response
        if resp is None or resp.status_code != 200:
            return False,\
                'HTTP request failed: API returned status %s (request path %s)' % (resp.status_code, path),\
                resp

        resp_json = resp.json()
        return True, None, resp_json

    def api_execute_order(self, order):
        # Should we even be executing this order?
        if order.amount < BITSTAMP_MINIMUM_TRADE_BTC:
            return False, 'Trade amount lower than MtGox minimum trade', None
        if order.status != 'N' or order.market_order_id != '':
            return False, 'Order has already been submitted to Bitstamp', None
        if not (order.currency_from.abbrev, order.currency_to.abbrev) in self.supported_currency_pairs:
            return False, 'Bitstamp does not support this currency pairing', None

        # Get the current trade fee associated with this account
        # success, err, fee = self.api_get_trade_fee()
        # if not success:
        #     return success, err, fee

        # Build the trade request
        trade_req = {}

        # type
        path = ''
        if order.order_type == 'B':
            path = 'buy'
        elif order.order_type == 'S':
            path = 'sell'
        else:
            return False, 'Unsupported order type: %s' % order.order_type, None

        # amount
        # TODO: Account for the trade fee here? Or somewhere else?
        trade_req['amount'] = order.amount

        # price
        new_price = 0
        if order.market_order:
            # API doesn't support true "market orders" - get the latest price and make
            # a limit order at that price. Also, force an update to make sure the price
            # isn't out of date and we don't get burnt
            success, err, current_price = self.api_get_current_market_price(force_update=True,
                                                                            currency_from=order.currency_from,
                                                                            currency_to=order.currency_to)
            if not success:
                return success, err, current_price

            if order.type == 'B':
                new_price = current_price.buy_price
            else:
                new_price = current_price.sell_price

            trade_req['price'] = new_price
        else:
            if order.price > 0:
                trade_req['price'] = order.price
            else:
                return False, 'Must specify a price for a non-market order', None

        # Send the trade request
        success, err, result = self.api_request(path=path,
                                                data=trade_req,
                                                post=True,
                                                add_credentials=True)
        if not success:
            return success, err, result

        # Save the order ID and update the status
        order.market_order_id = str(result['id'])
        if new_price > 0:
            order.price = new_price
        order.status = 'O'
        order.save()

        # Invalidate trade fee
        self.trade_fee_valid = False

        return True, None, None

    def api_get_current_market_price(self, force_update=False, currency_from=None, currency_to=None):
        # Wrangle the inputs - if we got currencies then use them, otherwise
        # set them to default values
        if currency_from is None or currency_to is None:
            currency_from = self.market.default_currency_from
            currency_to = self.market.default_currency_to

        if (currency_from.abbrev, currency_to.abbrev) not in self.supported_currency_pairs:
            return False, 'Currency pair not supported: %s%s' % (currency_from.abbrev, currency_to.abbrev), None

        # See if there's a recent price
        if not force_update:
            try:
                last_price = models.MarketPrice.objects.filter(
                    market=self.market,
                    currency_from=currency_from,
                    currency_to=currency_to
                ).order_by('-time')[0]

                # Is this price recent enough? If so, just return it
                if (timezone.now() - last_price.time).total_seconds() <= self.market_price_max_age:
                    return True, None, last_price

            except IndexError:
                # Don't do anything - this just means we couldn't find any MarketPrice
                # objects for the market/currency
                pass

        # If we got to this point, then we need to make an API call to get the latest price
        success, err, ticker = self.api_request(path='ticker/')
        if not success:
            return success, err, ticker

        # Build the MarketPrice object
        market_price = models.MarketPrice()
        market_price.market = self.market
        market_price.currency_from = currency_from
        market_price.currency_to = currency_to

        # Since we're on the opposite side of the transaction, the lowest "ask" price is
        # what we will be buying for, and vice versa
        market_price.buy_price = float(ticker['ask'])
        market_price.sell_price = float(ticker['bid'])

        # Save it so it can be "cached" for next time
        market_price.save()

        return True, None, market_price


# TODO: Check whether this is actually enforced by CampBX
CAMPBX_MINIMUM_TRADE_BTC = 0.01

CAMPBX_API_BASE_URL = 'https://CampBX.com/api/'


class CampBxMarket(MarketBase):
    """
    Market interface for CampBX
    https://www.campbx.com/

    Implemented based on the official documentation here:
    http://campbx.com/api.php

    No API keys - some functions require authentication using username/password
    """

    # CampBX only supports BTC/USD for a lot of API functions
    supported_currency_pairs = (
        ('BTC', 'USD'),
    )

    timeout = 15
    tryout = 5

    def __init__(self, market):
        super(CampBxMarket, self).__init__(market)

        self.api_user = settings.campbx_api_user
        self.api_password = settings.campbx_api_password

        # Internal storage for the current trading fee
        # Varies from account to account - must be updated via the API
        self.trade_fee = 0
        self.trade_fee_valid = False

        # Rolling window to limit requests made to the API
        # Max of 600 in 10 minutes
        self.reqs = {'max': 1, 'window': 0.5}
        self.req_timestamps = []

        # Default currency pair; used for API calls where the currency makes no difference
        self.default_currency_pair = self.market.default_currency_from.abbrev + self.market.default_currency_to.abbrev

        # Caching rules for market price data
        # If the last price is older than this number of seconds, an API call will be made to refresh the price
        self.market_price_max_age = 60

    def throttle(self):
        # Make sure we don't send more than a given number of requests in a certain
        # time window

        # First clear out old request timestamps
        current_timestamp = timezone.now()
        for timestamp in self.req_timestamps:
            if (current_timestamp - timestamp).total_seconds() > self.reqs['window']:
                self.req_timestamps.remove(timestamp)
            else:
                break

        # Now add the current timestamp
        self.req_timestamps.append(current_timestamp)

        # Now see if we have too many requests
        if len(self.req_timestamps) > self.reqs['max']:
            time.sleep(self.reqs['window'] - (current_timestamp - self.req_timestamps[0]).total_seconds())

    def api_request(self, path, post=False, add_credentials=False, data=None):
        # Convert input to a list if we got a dict
        if data is not None:
            if isinstance(data, dict):
                data = data.items()
        else:
            data = []

        if add_credentials:
            data.append(('user', self.api_user))
            data.append(('password', self.api_password))

        # Encode the data
        data_str = urllib.urlencode(data)

        # Build the headers
        headers = {
            'User-Agent': 'btctrader',
            'Accept-Encoding': 'gzip',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        tries = 0
        resp = None
        while tries < self.tryout:
            tries += 1

            # We want a hard throttle on requests to avoid being blocked
            self.throttle()

            # Make the actual request
            try:
                if post:
                    resp = requests.post(CAMPBX_API_BASE_URL + path,
                                         data=data_str,
                                         headers=headers,
                                         timeout=self.timeout)
                else:
                    resp = requests.get(CAMPBX_API_BASE_URL + path,
                                        data=data_str,
                                        headers=headers,
                                        timeout=self.timeout)
            except requests.Timeout:
                continue

            # If we got to here, the request did not timeout
            break

        # Check for failure response
        if resp is None or resp.status_code != 200:
            return False,\
                'HTTP request failed: API returned status %s (request path %s)' % (resp.status_code, path),\
                resp

        resp_json = resp.json()

        # Sometimes CampBX will throttle calls, or have some other error
        if 'Error' in resp_json.keys() and resp_json['Error'] != '':
            return False, 'API request failed: %s' % resp_json['Error'], None

        return True, None, resp_json

    def api_get_current_market_price(self, force_update=False, currency_from=None, currency_to=None):
        # Wrangle the inputs - if we got currencies then use them, otherwise
        # set them to default values
        if currency_from is None or currency_to is None:
            currency_from = self.market.default_currency_from
            currency_to = self.market.default_currency_to

        if (currency_from.abbrev, currency_to.abbrev) not in self.supported_currency_pairs:
            return False, 'Currency pair not supported: %s%s' % (currency_from.abbrev, currency_to.abbrev), None

        # See if there's a recent price
        if not force_update:
            try:
                last_price = models.MarketPrice.objects.filter(
                    market=self.market,
                    currency_from=currency_from,
                    currency_to=currency_to
                ).order_by('-time')[0]

                # Is this price recent enough? If so, just return it
                if (timezone.now() - last_price.time).total_seconds() <= self.market_price_max_age:
                    return True, None, last_price

            except IndexError:
                # Don't do anything - this just means we couldn't find any MarketPrice
                # objects for the market/currency
                pass

        # If we got to this point, then we need to make an API call to get the latest price
        success, err, ticker = self.api_request(path='xticker.php')
        if not success:
            return success, err, ticker

        # Build the MarketPrice object
        market_price = models.MarketPrice()
        market_price.market = self.market
        market_price.currency_from = currency_from
        market_price.currency_to = currency_to

        # Since we're on the opposite side of the transaction, the lowest "ask" price is
        # what we will be buying for, and vice versa
        market_price.buy_price = float(ticker['Best Bid'])
        market_price.sell_price = float(ticker['Best Ask'])

        # Save it so it can be "cached" for next time
        market_price.save()

        return True, None, market_price


class NullMarket(MarketBase):
    """
    Provides a dummy market interface that is not connected to a real market. Useful for testing purposes only.
    Has some varied behavior built into it based on random numbers, in order to simulate a variety of different
    scenarios - including random failures that might be seen on a real market.

    In a production deployment, you can safely remove this market from the AVAILABLE_MARKETS dictionary.
    """

    # Empty for now
    pass


# This is used for dynamically "reflecting" markets/orders to their corresponding API class
# Make sure to add new market classes to this dictionary when they're ready
# The key in this dictionary corresponds to models.Market.api_name
AVAILABLE_MARKETS = {
    'mtgox': MtGoxMarket,
    'bitstamp': BitstampMarket,
    'campbx': CampBxMarket,
    'null': NullMarket,
}