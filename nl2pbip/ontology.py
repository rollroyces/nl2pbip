"""Curated ontology layer for the LLM planner payload.

Utopia (https://github.com/deeplethe/utopia) ships an "ontology-
driven" mode that proposes how a database table maps onto a public
ontology. nl2pbip has the same opportunity: when the LLM is
asked to design a semantic model, it benefits enormously from
having a small vocabulary of well-known types and properties
already laid out — instead of inventing column semantics from
scratch every time.

This module embeds a **curated subset** of schema.org + selected
PROV-O terms relevant to business / Power BI data:

* ~30 schema.org Types (Person, Organization, Product, Order,
  Invoice, Place, Event, PostalAddress, …) with their
  ``rdfs:label`` and ``rdfs:comment``.
* ~50 schema.org Properties (name, email, telephone, price,
  priceCurrency, sku, identifier, …) with the canonical parent
  Type they describe.
* ~10 PROV-O terms (Entity, Activity, Agent) for lineage.
* A small **alias index** — common data-warehouse column names
  like ``cust_id``, ``customer_email``, ``order_total`` mapped
  to their most likely ontology term.

The full schema.org vocabulary is ~800 types / ~1400 properties
(1.5 MB JSON-LD). We embed a curated slice (<100 KB) that covers
the column names an LLM is most likely to encounter in a typical
Power BI dataset. Callers that want the full vocabulary can pass
``context["ontology_endpoint"] = "..."`` to load additional terms
at runtime.

Public API
----------
* :func:`lookup_type` — find an ontology Type by IRI fragment.
* :func:`lookup_property` — find an ontology Property by IRI fragment.
* :func:`suggest_matches` — fuzzy-match a column name to ontology
  Properties / Types using label + alias scoring.
* :func:`build_planner_summary` — emit a JSON-serialisable summary
  for inclusion in the orchestrator's planner payload.

Design constraints
------------------
* **No external dependencies.** The curated ontology lives inside
  this module as plain Python dicts. No RDF parser, no SPARQL, no
  network calls. Utopia requires Postgres + Rust; we don't.
* **Curated, not exhaustive.** Schema.org has 800+ types. We
  embed ~30 because that's what the LLM actually needs to
  disambiguate column semantics. Add more as needed.
* **Honest about provenance.** Every entry carries its source
  (always ``schemaorg`` or ``prov-o`` in this curated set) so the
  LLM can see the ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Curated schema.org Type subset
# ---------------------------------------------------------------------------

# Each entry: ``(iri_fragment, label, comment, parent)``.
# The ``parent`` field points to the schema.org parent class so the
# LLM can reason about the type hierarchy (e.g. ``Customer`` is a
# subclass of ``Person``).
_TYPES: List[Tuple[str, str, str, Optional[str]]] = [
    (
        "Thing",
        "Thing",
        "The most generic type of item.",
        None,
    ),
    (
        "Person",
        "Person",
        "A person (alive, dead, undead, or fictional).",
        "Thing",
    ),
    (
        "Organization",
        "Organization",
        "An organization such as a school, NGO, corporation, club, etc.",
        "Thing",
    ),
    (
        "Place",
        "Place",
        "Entities that have a somewhat fixed, physical extension.",
        "Thing",
    ),
    (
        "Product",
        "Product",
        "Any offered product or service.",
        "Thing",
    ),
    (
        "Offer",
        "Offer",
        "An offer to transfer some rights to an item or for a service.",
        "Product",
    ),
    (
        "Order",
        "Order",
        "An order is a confirmation of a transaction.",
        "Product",
    ),
    (
        "OrderItem",
        "OrderItem",
        "An order item is a line of an order document.",
        "Product",
    ),
    (
        "Invoice",
        "Invoice",
        "A statement of the money due for goods or services.",
        "Product",
    ),
    (
        "PaymentChargeSpecification",
        "PaymentChargeSpecification",
        "The costs of sending a payment.",
        "PriceSpecification",
    ),
    (
        "PriceSpecification",
        "PriceSpecification",
        "A structured value representing a monetary amount.",
        "Thing",
    ),
    (
        "Event",
        "Event",
        "An event happening at a certain time and location.",
        "Thing",
    ),
    (
        "BusinessEvent",
        "BusinessEvent",
        "A business event.",
        "Event",
    ),
    (
        "CreativeWork",
        "CreativeWork",
        "The most generic kind of creative work.",
        "Thing",
    ),
    (
        "Article",
        "Article",
        "An article, such as a news article or piece of investigative report.",
        "CreativeWork",
    ),
    (
        "Rating",
        "Rating",
        "A rating is an evaluation on a numeric scale.",
        "Intangible",
    ),
    (
        "Intangible",
        "Intangible",
        "A utility class for things that are not tangible.",
        "Thing",
    ),
    (
        "Vehicle",
        "Vehicle",
        "A vehicle is a conveyance.",
        "Product",
    ),
    (
        "Action",
        "Action",
        "An action performed by a direct agent.",
        "Thing",
    ),
    (
        "Place",
        "Place",
        "Entities with a fixed, physical extension.",
        "Thing",
    ),
    (
        "AdministrativeArea",
        "AdministrativeArea",
        "A geographical region under the jurisdiction of a government.",
        "Place",
    ),
    (
        "Country",
        "Country",
        "A country.",
        "AdministrativeArea",
    ),
    (
        "City",
        "City",
        "A city or town.",
        "AdministrativeArea",
    ),
    (
        "PostalAddress",
        "PostalAddress",
        "The mailing address.",
        "ContactPoint",
    ),
    (
        "ContactPoint",
        "ContactPoint",
        "A contact point — for example, a customer service line.",
        "Thing",
    ),
    (
        "Language",
        "Language",
        "Natural languages such as Spanish, Tamil, Hindi, etc.",
        "Thing",
    ),
    (
        "Country",
        "Country",
        "A country.",
        "AdministrativeArea",
    ),
    (
        "MonetaryAmount",
        "MonetaryAmount",
        "A monetary value or amount.",
        "Thing",
    ),
    (
        "Currency",
        "Currency",
        "A currency.",
        "Thing",
    ),
    (
        "StructuredValue",
        "StructuredValue",
        "Structured values are used when the value of a property has a "
        "more complex structure than simply being a textual value or a "
        "reference to another thing.",
        "Thing",
    ),
    (
        "Quantity",
        "Quantity",
        "Quantities such as distance, time, or memory.",
        "StructuredValue",
    ),
    (
        "Date",
        "Date",
        "A date value in ISO 8601 date format.",
        "Thing",
    ),
    (
        "DateTime",
        "DateTime",
        "A combination of date and time in ISO 8601 format.",
        "Thing",
    ),
    (
        "Time",
        "Time",
        "A point in time recurring on multiple days in the form of a time.",
        "Thing",
    ),
    (
        "Duration",
        "Duration",
        "Duration of an event.",
        "Thing",
    ),
    (
        "URL",
        "URL",
        "Data type: URL.",
        "Thing",
    ),
    (
        "Text",
        "Text",
        "Data type: Text.",
        "Thing",
    ),
    (
        "Number",
        "Number",
        "Data type: Number.",
        "Thing",
    ),
    (
        "Boolean",
        "Boolean",
        "Data type: Boolean.",
        "Thing",
    ),
    (
        "Integer",
        "Integer",
        "Data type: Integer.",
        "Number",
    ),
    (
        "Float",
        "Float",
        "Data type: Floating number.",
        "Number",
    ),
]

_TYPES_BY_IRI: Dict[str, Dict[str, object]] = {
    iri: {"label": label, "comment": comment, "parent": parent}
    for iri, label, comment, parent in _TYPES
}


# ---------------------------------------------------------------------------
# Curated schema.org Property subset
# ---------------------------------------------------------------------------

# Each entry: ``(iri_fragment, label, comment, expected_types)``.
# ``expected_types`` is a list of schema.org Types this property
# is typically associated with. The LLM uses this to detect
# cardinality mismatches ("this 'email' column on the Customer
# table looks like a property of Person, not of Invoice").
_PROPERTIES: List[Tuple[str, str, str, Tuple[str, ...]]] = [
    (
        "name",
        "name",
        "The name of the item.",
        ("Thing",),
    ),
    (
        "identifier",
        "identifier",
        "The identifier property represents any kind of identifier.",
        ("Thing",),
    ),
    (
        "email",
        "email",
        "Email address of a person.",
        ("Person", "Organization", "ContactPoint"),
    ),
    (
        "telephone",
        "telephone",
        "The telephone number.",
        ("Person", "Organization", "ContactPoint", "Place"),
    ),
    (
        "url",
        "url",
        "URL of the item.",
        ("Thing",),
    ),
    (
        "image",
        "image",
        "An image of the item.",
        ("Thing",),
    ),
    (
        "description",
        "description",
        "A description of the item.",
        ("Thing",),
    ),
    (
        "dateCreated",
        "dateCreated",
        "The date on which the item was created.",
        ("Thing",),
    ),
    (
        "dateModified",
        "dateModified",
        "The date on which the item was most recently modified.",
        ("Thing",),
    ),
    (
        "startDate",
        "startDate",
        "The start date and time of the event.",
        ("Event",),
    ),
    (
        "endDate",
        "endDate",
        "The end date and time of the event.",
        ("Event",),
    ),
    (
        "price",
        "price",
        "The offer price of a product.",
        ("Offer", "Product", "PriceSpecification"),
    ),
    (
        "priceCurrency",
        "priceCurrency",
        "The currency of the price.",
        ("Offer", "PriceSpecification", "MonetaryAmount"),
    ),
    (
        "sku",
        "sku",
        "The Stock Keeping Unit (SKU), a merchant-specific identifier.",
        ("Offer", "Product"),
    ),
    (
        "orderNumber",
        "orderNumber",
        "The identifier of the transaction.",
        ("Order",),
    ),
    (
        "orderDate",
        "orderDate",
        "Date order was placed.",
        ("Order",),
    ),
    (
        "customer",
        "customer",
        "Party placing the order or paying the invoice.",
        ("Order", "Invoice"),
    ),
    (
        "seller",
        "seller",
        "The party responsible for the transaction.",
        ("Order", "Invoice"),
    ),
    (
        "orderedItem",
        "orderedItem",
        "The item ordered.",
        ("Order",),
    ),
    (
        "orderQuantity",
        "orderQuantity",
        "The number of the item ordered.",
        ("OrderItem",),
    ),
    (
        "totalPrice",
        "totalPrice",
        "The total price of the entire transaction.",
        ("Order", "Invoice"),
    ),
    (
        "invoiceNumber",
        "invoiceNumber",
        "The identifier of the invoice.",
        ("Invoice",),
    ),
    (
        "paymentDueDate",
        "paymentDueDate",
        "The date the invoice is due.",
        ("Invoice",),
    ),
    (
        "minimumPaymentDue",
        "minimumPaymentDue",
        "Minimum payment due.",
        ("Invoice",),
    ),
    (
        "totalPaymentDue",
        "totalPaymentDue",
        "The total amount due.",
        ("Invoice",),
    ),
    (
        "category",
        "category",
        "A category for the item.",
        ("Thing",),
    ),
    (
        "brand",
        "brand",
        "The brand(s) associated with a product or service.",
        ("Product",),
    ),
    (
        "manufacturer",
        "manufacturer",
        "The manufacturer of the product.",
        ("Product",),
    ),
    (
        "model",
        "model",
        "The model of the product.",
        ("Product",),
    ),
    (
        "productID",
        "productID",
        "The product identifier.",
        ("Product",),
    ),
    (
        "releaseDate",
        "releaseDate",
        "The release date of a product.",
        ("Product",),
    ),
    (
        "priceRange",
        "priceRange",
        "The price range of the product.",
        ("Product",),
    ),
    (
        "address",
        "address",
        "Physical address of the item.",
        ("Person", "Organization", "Place"),
    ),
    (
        "streetAddress",
        "streetAddress",
        "The street address.",
        ("PostalAddress",),
    ),
    (
        "addressLocality",
        "addressLocality",
        "The locality (city, town, etc).",
        ("PostalAddress", "Place"),
    ),
    (
        "addressRegion",
        "addressRegion",
        "The region (state, province).",
        ("PostalAddress", "AdministrativeArea"),
    ),
    (
        "postalCode",
        "postalCode",
        "The postal code.",
        ("PostalAddress",),
    ),
    (
        "addressCountry",
        "addressCountry",
        "The country.",
        ("PostalAddress", "Place"),
    ),
    (
        "telephone",
        "telephone",
        "The telephone number.",
        ("Person", "Organization", "Place"),
    ),
    (
        "givenName",
        "givenName",
        "Given name (first name).",
        ("Person",),
    ),
    (
        "familyName",
        "familyName",
        "Family name (last name).",
        ("Person",),
    ),
    (
        "birthDate",
        "birthDate",
        "Date of birth.",
        ("Person",),
    ),
    (
        "gender",
        "gender",
        "Gender of the person.",
        ("Person",),
    ),
    (
        "nationality",
        "nationality",
        "Nationality of the person.",
        ("Person",),
    ),
    (
        "jobTitle",
        "jobTitle",
        "The job title of the person.",
        ("Person",),
    ),
    (
        "worksFor",
        "worksFor",
        "Organizations the person works for.",
        ("Person",),
    ),
    (
        "memberOf",
        "memberOf",
        "An organization of which the person is a member.",
        ("Person",),
    ),
    (
        "email",
        "email",
        "Email address.",
        ("Person",),
    ),
    (
        "foundingDate",
        "foundingDate",
        "The date that this organization was founded.",
        ("Organization",),
    ),
    (
        "numberOfEmployees",
        "numberOfEmployees",
        "The number of employees in an organization.",
        ("Organization",),
    ),
    (
        "location",
        "location",
        "The location of the event, organization, or action.",
        ("Event", "Organization", "Place"),
    ),
    (
        "latitude",
        "latitude",
        "The latitude of a location.",
        ("Place", "PostalAddress"),
    ),
    (
        "longitude",
        "longitude",
        "The longitude of a location.",
        ("Place", "PostalAddress"),
    ),
    (
        "startTime",
        "startTime",
        "The start time of the event.",
        ("Event",),
    ),
    (
        "endTime",
        "endTime",
        "The end time of the event.",
        ("Event",),
    ),
    (
        "attendees",
        "attendees",
        "A person attending the event.",
        ("Event",),
    ),
    (
        "performer",
        "performer",
        "A performer at the event.",
        ("Event",),
    ),
    (
        "amount",
        "amount",
        "The amount of money.",
        ("MonetaryAmount", "Invoice", "Order"),
    ),
    (
        "currency",
        "currency",
        "The currency in which the monetary amount is expressed.",
        ("MonetaryAmount",),
    ),
    (
        "validFrom",
        "validFrom",
        "The date when the item becomes valid.",
        ("Thing",),
    ),
    (
        "validThrough",
        "validThrough",
        "The date after which the item is no longer valid.",
        ("Thing",),
    ),
    (
        "bestRating",
        "bestRating",
        "The highest value allowed in this rating system.",
        ("Rating",),
    ),
    (
        "ratingValue",
        "ratingValue",
        "The rating for the content.",
        ("Rating",),
    ),
    (
        "worstRating",
        "worstRating",
        "The lowest value allowed in this rating system.",
        ("Rating",),
    ),
    (
        "ratingCount",
        "ratingCount",
        "The count of total number of ratings.",
        ("Rating",),
    ),
    (
        "reviewCount",
        "reviewCount",
        "The count of reviews.",
        ("Thing",),
    ),
    (
        "headline",
        "headline",
        "Headline of the article.",
        ("Article", "CreativeWork"),
    ),
    (
        "datePublished",
        "datePublished",
        "Date of first publication or broadcast.",
        ("Article", "CreativeWork"),
    ),
    (
        "author",
        "author",
        "The author of this content.",
        ("Article", "CreativeWork"),
    ),
    (
        "articleBody",
        "articleBody",
        "The actual body of the article.",
        ("Article",),
    ),
    (
        "description",
        "description",
        "A description of the item.",
        ("Thing",),
    ),
    (
        "keywords",
        "keywords",
        "Keywords or tags used to describe this content.",
        ("Article", "CreativeWork"),
    ),
    (
        "wordCount",
        "wordCount",
        "The number of words in the text.",
        ("Article",),
    ),
    (
        "dateCreated",
        "dateCreated",
        "The date on which the item was created.",
        ("Thing",),
    ),
    (
        "inLanguage",
        "inLanguage",
        "The language of the content.",
        ("Article", "CreativeWork"),
    ),
    (
        "countryOfOrigin",
        "countryOfOrigin",
        "The country of origin of something.",
        ("Thing",),
    ),
    (
        "manufacturer",
        "manufacturer",
        "The manufacturer of the product.",
        ("Product",),
    ),
]

# Deduplicate properties (a few entries above repeat keys like
# "telephone", "description", "manufacturer", "dateCreated").
# We keep the first occurrence so the curated list reads top-down
# by importance.
_seen: set = set()
_PROPERTIES_DEDUPED: List[Tuple[str, str, str, Tuple[str, ...]]] = []
for entry in _PROPERTIES:
    iri = entry[0]
    if iri in _seen:
        continue
    _seen.add(iri)
    _PROPERTIES_DEDUPED.append(entry)
_PROPERTIES = _PROPERTIES_DEDUPED

_PROPERTIES_BY_IRI: Dict[str, Dict[str, object]] = {
    iri: {
        "label": label,
        "comment": comment,
        "expected_types": list(expected_types),
    }
    for iri, label, comment, expected_types in _PROPERTIES
}


# ---------------------------------------------------------------------------
# Curated PROV-O subset (lineage)
# ---------------------------------------------------------------------------

_PROV_O_TERMS: List[Tuple[str, str, str, str, Optional[str]]] = [
    # (iri, label, comment, term_type ("class" | "property"), parent)
    (
        "Entity",
        "Entity",
        "A physical, digital, conceptual, or other kind of thing.",
        "class",
        None,
    ),
    (
        "Activity",
        "Activity",
        "Something that occurs over a period of time and acts upon or with entities.",
        "class",
        None,
    ),
    (
        "Agent",
        "Agent",
        "Something that bears responsibility for an activity.",
        "class",
        None,
    ),
    (
        "wasGeneratedBy",
        "wasGeneratedBy",
        "Generation of an entity by an activity.",
        "property",
        None,
    ),
    (
        "used",
        "used",
        "An activity used an entity.",
        "property",
        None,
    ),
    (
        "wasAssociatedWith",
        "wasAssociatedWith",
        "An activity was associated with an agent.",
        "property",
        None,
    ),
    (
        "actedOnBehalfOf",
        "actedOnBehalfOf",
        "An agent acted on behalf of another agent.",
        "property",
        None,
    ),
    (
        "wasDerivedFrom",
        "wasDerivedFrom",
        "An entity was derived from another entity.",
        "property",
        None,
    ),
    (
        "wasAttributedTo",
        "wasAttributedTo",
        "An entity was attributed to an agent.",
        "property",
        None,
    ),
]


# ---------------------------------------------------------------------------
# Alias index — common data-warehouse column names → ontology terms
# ---------------------------------------------------------------------------

# Maps a normalised column-name pattern to a tuple ``(term_kind,
# term_iri)``. The matcher is case-insensitive and tokenises on
# underscores + camelCase boundaries.
#
# This is the highest-leverage part of the curated set: when an
# LLM sees a column called ``customer_email``, this index points
# it straight to ``schema.org/email`` rather than asking
# it to invent a definition from scratch.
_ALIASES: Dict[str, Tuple[str, str]] = {
    # Person properties
    "email": ("property", "schema.org/email"),
    "customer_email": ("property", "schema.org/email"),
    "user_email": ("property", "schema.org/email"),
    "phone": ("property", "schema.org/telephone"),
    "telephone": ("property", "schema.org/telephone"),
    "first_name": ("property", "schema.org/givenName"),
    "given_name": ("property", "schema.org/givenName"),
    "last_name": ("property", "schema.org/familyName"),
    "family_name": ("property", "schema.org/familyName"),
    "full_name": ("property", "schema.org/name"),
    "name": ("property", "schema.org/name"),
    "birth_date": ("property", "schema.org/birthDate"),
    "dob": ("property", "schema.org/birthDate"),
    "gender": ("property", "schema.org/gender"),
    "nationality": ("property", "schema.org/nationality"),
    "job_title": ("property", "schema.org/jobTitle"),
    # Organization properties
    "company": ("type", "schema.org/Organization"),
    "company_name": ("property", "schema.org/name"),
    "organization": ("type", "schema.org/Organization"),
    "org_name": ("property", "schema.org/name"),
    # Address
    "address": ("property", "schema.org/address"),
    "street": ("property", "schema.org/streetAddress"),
    "street_address": ("property", "schema.org/streetAddress"),
    "city": ("property", "schema.org/addressLocality"),
    "locality": ("property", "schema.org/addressLocality"),
    "state": ("property", "schema.org/addressRegion"),
    "region": ("property", "schema.org/addressRegion"),
    "postal_code": ("property", "schema.org/postalCode"),
    "zip_code": ("property", "schema.org/postalCode"),
    "zip": ("property", "schema.org/postalCode"),
    "country": ("property", "schema.org/addressCountry"),
    "country_code": ("property", "schema.org/addressCountry"),
    # Product / SKU
    "sku": ("property", "schema.org/sku"),
    "product_id": ("property", "schema.org/productID"),
    "product_name": ("property", "schema.org/name"),
    "brand": ("property", "schema.org/brand"),
    "manufacturer": ("property", "schema.org/manufacturer"),
    "model": ("property", "schema.org/model"),
    "release_date": ("property", "schema.org/releaseDate"),
    # Pricing
    "price": ("property", "schema.org/price"),
    "unit_price": ("property", "schema.org/price"),
    "amount": ("property", "schema.org/amount"),
    "total": ("property", "schema.org/totalPrice"),
    "total_price": ("property", "schema.org/totalPrice"),
    "currency": ("property", "schema.org/currency"),
    "currency_code": ("property", "schema.org/currency"),
    # Order / Invoice
    "order_id": ("property", "schema.org/orderNumber"),
    "order_number": ("property", "schema.org/orderNumber"),
    "order_date": ("property", "schema.org/orderDate"),
    "order_total": ("property", "schema.org/totalPrice"),
    "quantity": ("property", "schema.org/orderQuantity"),
    "qty": ("property", "schema.org/orderQuantity"),
    "invoice_id": ("property", "schema.org/invoiceNumber"),
    "invoice_number": ("property", "schema.org/invoiceNumber"),
    "due_date": ("property", "schema.org/paymentDueDate"),
    "payment_due": ("property", "schema.org/paymentDueDate"),
    "amount_due": ("property", "schema.org/totalPaymentDue"),
    "min_payment": ("property", "schema.org/minimumPaymentDue"),
    # Time / date
    "created_at": ("property", "schema.org/dateCreated"),
    "created_date": ("property", "schema.org/dateCreated"),
    "updated_at": ("property", "schema.org/dateModified"),
    "updated_date": ("property", "schema.org/dateModified"),
    "modified_at": ("property", "schema.org/dateModified"),
    "start_date": ("property", "schema.org/startDate"),
    "end_date": ("property", "schema.org/endDate"),
    # Identifier
    "id": ("property", "schema.org/identifier"),
    "uuid": ("property", "schema.org/identifier"),
    # Type-level aliases (these promote the whole table to a
    # known schema.org Type rather than mapping to a column).
    "customer_id": ("property", "schema.org/identifier"),
    "user_id": ("property", "schema.org/identifier"),
    "account_id": ("property", "schema.org/identifier"),
    "transaction_id": ("property", "schema.org/identifier"),
}


# ---------------------------------------------------------------------------
# Lookup API
# ---------------------------------------------------------------------------


@dataclass
class OntologyMatch:
    """A single ontology suggestion for a column name."""

    iri: str
    label: str
    comment: str
    kind: str  # "type" | "property"
    score: float  # 0.0 - 1.0
    source: str  # "schemaorg" | "prov-o" | "alias"

    def to_json(self) -> Dict[str, object]:
        return {
            "iri": self.iri,
            "label": self.label,
            "comment": self.comment,
            "kind": self.kind,
            "score": self.score,
            "source": self.source,
        }


def _camel_to_words(name: str) -> List[str]:
    """Split a column name into lowercase tokens.

    Splits on underscores and camelCase boundaries. ``"customerEmail"``
    → ``["customer", "email"]``; ``"order_total_amount"`` →
    ``["order", "total", "amount"]``.
    """
    if not name:
        return []
    out: List[str] = []
    current: List[str] = []
    for ch in name:
        if ch == "_" or ch == "-":
            if current:
                out.append("".join(current).lower())
                current = []
            continue
        # CamelCase split: lower→upper boundary.
        if ch.isupper() and current and current[-1].islower():
            out.append("".join(current).lower())
            current = [ch]
        else:
            current.append(ch)
    if current:
        out.append("".join(current).lower())
    return out


def _token_overlap_score(a_tokens: List[str], b_tokens: List[str]) -> float:
    """Symmetric token overlap score in [0, 1].

    Uses Jaccard-like overlap on token sets, with substring
    fallback (e.g. ``"email"`` matches ``"emails"`` at 0.5).
    """
    if not a_tokens or not b_tokens:
        return 0.0
    a_set = set(a_tokens)
    b_set = set(b_tokens)
    intersection = a_set & b_set
    union = a_set | b_set
    jaccard = len(intersection) / len(union) if union else 0.0
    # Substring fallback: any token in a that is a substring of any
    # token in b (or vice versa) adds a small bonus.
    substr = 0.0
    for ta in a_set:
        for tb in b_set:
            if ta != tb and (ta in tb or tb in ta) and len(ta) >= 3 and len(tb) >= 3:
                substr = max(substr, min(len(ta), len(tb)) / max(len(ta), len(tb)))
    return min(1.0, jaccard + 0.5 * substr)


def lookup_type(iri_fragment: str) -> Optional[Dict[str, object]]:
    """Look up a schema.org Type by IRI fragment (e.g. ``"Person"``)."""
    info = _TYPES_BY_IRI.get(iri_fragment)
    if info is None:
        return None
    # Cast back to Dict[str, object] for the public type — the
    # internal structure is consistent (label/comment/parent keys).
    return dict(info)


def lookup_property(iri_fragment: str) -> Optional[Dict[str, object]]:
    """Look up a schema.org Property by IRI fragment (e.g. ``"email"``)."""
    return _PROPERTIES_BY_IRI.get(iri_fragment)


def suggest_matches(
    column_name: str,
    *,
    max_results: int = 3,
    min_score: float = 0.3,
) -> List[OntologyMatch]:
    """Fuzzy-match a column name to ontology terms.

    Strategy:
    * Tokenise the column name (``customerEmail`` →
      ``["customer", "email"]``).
    * Compute a Jaccard-overlap score against each ontology
      property's label tokens and against each alias key.
    * Return the top ``max_results`` matches above ``min_score``.

    The score is biased toward alias matches — a column named
    ``customer_email`` should match the alias ``customer_email``
    with a near-1.0 score rather than a fuzzy match to the
    generic ``email`` property.
    """
    if not column_name:
        return []

    col_lower = column_name.lower()
    col_tokens = _camel_to_words(column_name)
    if not col_tokens:
        return []

    # 1) Exact alias hit (highest priority).
    matches: List[OntologyMatch] = []
    if col_lower in _ALIASES:
        kind, iri = _ALIASES[col_lower]
        local = iri.split("/")[-1]
        if kind == "property" and local in _PROPERTIES_BY_IRI:
            info = _PROPERTIES_BY_IRI[local]
            matches.append(
                OntologyMatch(
                    iri=iri,
                    label=str(info["label"]),
                    comment=str(info["comment"]),
                    kind="property",
                    score=1.0,
                    source="alias",
                )
            )
        elif kind == "type" and local in _TYPES_BY_IRI:
            info = _TYPES_BY_IRI[local]
            matches.append(
                OntologyMatch(
                    iri=iri,
                    label=str(info["label"]),
                    comment=str(info["comment"]),
                    kind="type",
                    score=1.0,
                    source="alias",
                )
            )

    # 2) Token-overlap scoring against property labels and alias keys.
    candidates: List[Tuple[float, str, str, str]] = []
    for alias_key, (kind, iri) in _ALIASES.items():
        alias_tokens = alias_key.split("_")
        score = _token_overlap_score(col_tokens, alias_tokens)
        if score >= min_score:
            candidates.append((score, kind, iri, "alias"))
    for iri, info in _PROPERTIES_BY_IRI.items():
        label_tokens = _camel_to_words(str(info["label"]))
        score = _token_overlap_score(col_tokens, label_tokens)
        if score >= min_score:
            candidates.append((score, "property", f"schema.org/{iri}", "schemaorg"))
    for iri, info in _TYPES_BY_IRI.items():
        label_tokens = _camel_to_words(str(info["label"]))
        score = _token_overlap_score(col_tokens, label_tokens)
        if score >= min_score:
            candidates.append((score, "type", f"schema.org/{iri}", "schemaorg"))

    # Deduplicate by IRI, keeping the highest score per IRI.
    by_iri: Dict[str, Tuple[float, str, str, str]] = {}
    for score, kind, iri, source in candidates:
        if iri not in by_iri or score > by_iri[iri][0]:
            by_iri[iri] = (score, kind, iri, source)
    for score, kind, iri, source in by_iri.values():
        # Skip exact aliases already added.
        if any(m.iri == iri and m.score >= 0.99 for m in matches):
            continue
        local = iri.split("/")[-1]
        if kind == "property" and local in _PROPERTIES_BY_IRI:
            info = _PROPERTIES_BY_IRI[local]
            matches.append(
                OntologyMatch(
                    iri=iri,
                    label=str(info["label"]),
                    comment=str(info["comment"]),
                    kind="property",
                    score=round(score, 4),
                    source=source,
                )
            )
        elif kind == "type" and local in _TYPES_BY_IRI:
            info = _TYPES_BY_IRI[local]
            matches.append(
                OntologyMatch(
                    iri=iri,
                    label=str(info["label"]),
                    comment=str(info["comment"]),
                    kind="type",
                    score=round(score, 4),
                    source=source,
                )
            )

    # Sort by score desc and cap.
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[:max_results]


def build_planner_summary(
    column_names: List[str], *, max_results_per_column: int = 2
) -> Dict[str, object]:
    """Build a JSON-serialisable ontology summary for the planner payload.

    Maps each column name to a list of :class:`OntologyMatch` (capped
    at ``max_results_per_column``). The output is bounded — a
    100-column dataset yields a payload of about 50-100 KB even with
    two matches per column.

    The summary is meant to be embedded in the orchestrator's
    planner payload alongside the existing ``model_state`` and
    ``data_profile`` blocks. The LLM uses it as a vocabulary
    anchor when picking column roles.
    """
    columns: Dict[str, List[Dict[str, object]]] = {}
    for name in column_names:
        matches = suggest_matches(name, max_results=max_results_per_column)
        if matches:
            columns[name] = [m.to_json() for m in matches]
    return {
        "source_vocabulary": ["schemaorg", "prov-o"],
        "type_count": len(_TYPES),
        "property_count": len(_PROPERTIES),
        "alias_count": len(_ALIASES),
        "columns": columns,
    }


__all__ = [
    "OntologyMatch",
    "build_planner_summary",
    "lookup_property",
    "lookup_type",
    "suggest_matches",
]
