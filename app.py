"""Flask web dashboard for the 3D Printer Kijiji Deal Tracker."""

import json

from flask import Flask, render_template, request, jsonify

import db
from tracker import compute_deals

app = Flask(__name__)


@app.before_request
def ensure_db():
    db.init_db()


@app.template_filter("from_json")
def from_json_filter(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


@app.route("/")
def index():
    filters = {
        "brand": request.args.get("brand"),
        "min_price": request.args.get("min_price", type=float),
        "max_price": request.args.get("max_price", type=float),
        "location": request.args.get("location"),
        "search": request.args.get("search"),
        "active_only": request.args.get("active_only", "1") == "1",
        "sort_by": request.args.get("sort_by", "last_seen"),
    }
    listings = db.get_listings(filters)
    brands = db.get_distinct_brands()
    stats = db.get_stats()
    return render_template("index.html", listings=listings, brands=brands,
                           filters=filters, stats=stats)


@app.route("/listing/<kijiji_id>")
def listing_detail(kijiji_id):
    listing = db.get_listing(kijiji_id)
    if not listing:
        return "Listing not found", 404
    price_history = db.get_price_history(kijiji_id)
    return render_template("listing.html", listing=listing, price_history=price_history)


@app.route("/deals")
def deals():
    listings = db.get_listings({"active_only": True})
    deal_list = compute_deals(listings)
    return render_template("deals.html", deals=deal_list)


@app.route("/api/price-history/<kijiji_id>")
def api_price_history(kijiji_id):
    history = db.get_price_history(kijiji_id)
    return jsonify({
        "dates": [h["scraped_at"][:10] for h in history],
        "prices": [h["price"] for h in history],
    })


if __name__ == "__main__":
    app.run(debug=True)
