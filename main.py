"""Main application orchestration pipeline for the Football News Automation system."""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from core.deduplication import DeduplicationEngine
from core.logging_config import setup_logger
from core.models import ProcessedPost, ProcessingStatus, DailyStats
from core.scheduler import Scheduler
from services.facebook_service import FacebookPublishingService
from services.image_service import ImageService
from services.llm_service import LLMService
from services.rss_service import RSSService
from services.verification_service import VerificationService
from storage.json_storage import JsonStorage


def run_pipeline() -> None:
    """Executes a single cycle of the football news publishing pipeline."""
    # 1. Setup Logger with secrets redaction
    logger = setup_logger(
        explicit_secrets=[settings.OPENAI_API_KEY, settings.FACEBOOK_PAGE_TOKEN]
    )
    logger.info("=" * 60)
    logger.info("Starting Autonomous Football News Publishing Cycle")
    logger.info(f"Mode: {'[DRY RUN]' if settings.DRY_RUN else '[LIVE PUBLISHING]'}")
    logger.info(f"Target Daily Limit: {settings.DAILY_LIMIT} | Max Posts Per Run: {settings.MAX_POSTS_PER_RUN}")
    logger.info("=" * 60)

    # 2. Initialize Persistent Storage
    storage = JsonStorage(file_path=settings.STATE_FILE)
    storage.initialize()

    # 3. Initialize Services
    rss_service = RSSService(
        config_path=settings.FEEDS_CONFIG_PATH,
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
        max_age_hours=settings.MAX_ARTICLE_AGE_HOURS,
        user_agent=settings.USER_AGENT
    )
    llm_service = LLMService(
        api_key=settings.OPENAI_API_KEY,
        model=settings.OPENAI_MODEL,
        temperature=settings.LLM_TEMPERATURE,
        max_retries=settings.MAX_LLM_RETRIES
    )
    verification_service = VerificationService(
        confidence_threshold=settings.LLM_CONFIDENCE_THRESHOLD
    )
    image_service = ImageService(
        fallback_path=settings.FALLBACK_IMAGE_PATH,
        pexels_api_key=settings.PEXELS_API_KEY,
        unsplash_access_key=settings.UNSPLASH_ACCESS_KEY,
        use_stock_images=settings.USE_STOCK_IMAGES,
        timeout=settings.REQUEST_TIMEOUT_SECONDS
    )
    fb_service = FacebookPublishingService(
        page_id=settings.FACEBOOK_PAGE_ID,
        page_token=settings.FACEBOOK_PAGE_TOKEN,
        api_version=settings.GRAPH_API_VERSION,
        dry_run=settings.DRY_RUN,
        timeout=settings.REQUEST_TIMEOUT_SECONDS
    )
    scheduler = Scheduler(
        daily_limit=settings.DAILY_LIMIT,
        max_posts_per_run=settings.MAX_POSTS_PER_RUN,
        min_interval_minutes=settings.MIN_POST_INTERVAL_MINUTES,
        timezone_str=settings.TIMEZONE,
        quiet_hours_start=settings.QUIET_HOURS_START,
        quiet_hours_end=settings.QUIET_HOURS_END,
        category_limits=settings.CATEGORY_DAILY_LIMITS
    )
    dedup_engine = DeduplicationEngine(similarity_threshold=0.75)

    # 4. Check Daily Capacity in Local Cairo Time
    today_cairo = scheduler.get_current_cairo_date_str()
    daily_stats = storage.get_daily_stats(today_cairo)

    if scheduler.is_in_quiet_period():
        logger.info(f"Local time in Cairo ({settings.TIMEZONE}) is in quiet period. Sleeping until morning.")
        return

    if daily_stats.posts_published >= settings.DAILY_LIMIT:
        logger.info(f"Daily post limit reached for today ({daily_stats.posts_published}/{settings.DAILY_LIMIT}). Halting.")
        return

    # 5. RSS Ingestion
    discovered_articles = rss_service.fetch_all()
    daily_stats.articles_discovered += len(discovered_articles)
    logger.info(f"Discovered {len(discovered_articles)} candidate articles from RSS feeds.")

    # 6. Prioritize Articles
    prioritized_articles = scheduler.prioritize_articles(discovered_articles)

    seen_hashes = storage.get_all_seen_hashes()
    seen_urls = storage.get_all_seen_urls()
    recent_titles = storage.get_recent_titles(limit=150)
    used_images = storage.get_used_image_ids()

    posts_published_in_run = 0

    # 7. Processing Loop
    for article in prioritized_articles:
        if posts_published_in_run >= settings.MAX_POSTS_PER_RUN:
            logger.info(f"Reached run limit of {settings.MAX_POSTS_PER_RUN} posts. Ending run cycle.")
            break

        # Deduplication check
        is_dup, dup_reason = dedup_engine.is_duplicate(
            candidate_hash=article.content_hash,
            candidate_url=article.canonical_url,
            candidate_title=article.title,
            candidate_entities=None,
            seen_hashes=seen_hashes,
            seen_urls=seen_urls,
            recent_titles=recent_titles
        )

        if is_dup:
            logger.debug(f"Skipping duplicate article '{article.title[:40]}': {dup_reason}")
            daily_stats.duplicates_skipped += 1
            continue

        # Record discovery in state
        storage.record_discovered(
            content_hash=article.content_hash,
            canonical_url=article.canonical_url,
            title=article.title,
            source=article.source_name
        )
        seen_hashes.add(article.content_hash)
        seen_urls.add(article.canonical_url)

        # LLM Analysis & Egyptian Arabic Caption Generation
        logger.info(f"Analyzing article with LLM ({settings.OPENAI_MODEL}): '{article.title}'")
        analysis, tokens = llm_service.analyze_article(article)
        daily_stats.estimated_llm_tokens += tokens

        if not analysis:
            logger.warning(f"LLM processing failed for '{article.title}'.")
            storage.record_failure(article.content_hash, "LLM analysis failed")
            continue

        if not analysis.eligible:
            logger.info(f"Article rejected by LLM: {analysis.rejection_reason}")
            daily_stats.articles_rejected += 1
            continue

        # Scheduler Capacity & Category quota check
        eligible, sched_reason = scheduler.check_eligibility(daily_stats, analysis.category)
        if not eligible:
            logger.info(f"Skipping article due to scheduler quota: {sched_reason}")
            continue

        # Fact-checking, Verification, & Caption Validation
        verified, verif_reason, calibrated_analysis = verification_service.verify(article, analysis)
        if not verified:
            logger.warning(f"Verification rejected article '{article.title}': {verif_reason}")
            storage.record_failure(article.content_hash, f"Verification failed: {verif_reason}")
            daily_stats.articles_rejected += 1
            continue

        daily_stats.captions_generated += 1

        # Check Manual Approval Flag
        if settings.APPROVAL_REQUIRED:
            logger.info(f"[APPROVAL REQUIRED] Post staged for approval: '{article.title}'")
            continue

        # Licensed Image Selection
        selected_image = image_service.select_image(
            entities=calibrated_analysis.entities,
            category=calibrated_analysis.category,
            used_image_ids=used_images,
            article_image_url=article.image_url
        )
        if selected_image.image_id:
            storage.record_used_image_id(selected_image.image_id)
            used_images.add(selected_image.image_id)

        # Facebook Meta Publishing
        logger.info(f"Publishing to Facebook Page: '{article.title}'")
        success, post_id, pub_error = fb_service.publish_post(
            caption=calibrated_analysis.caption,
            image=selected_image
        )

        post_record = ProcessedPost(
            article_hash=article.content_hash,
            canonical_url=article.canonical_url,
            original_title=article.title,
            source_name=article.source_name,
            category=calibrated_analysis.category,
            priority=calibrated_analysis.priority,
            caption=calibrated_analysis.caption,
            image=selected_image,
            status=ProcessingStatus.PUBLISHED if success else ProcessingStatus.PUBLISH_FAILED,
            post_id=post_id,
            error_message=pub_error,
            published_at=datetime.now(timezone.utc) if success else None
        )

        if success:
            posts_published_in_run += 1
            daily_stats.posts_published += 1
            daily_stats.category_counts[calibrated_analysis.category] = (
                daily_stats.category_counts.get(calibrated_analysis.category, 0) + 1
            )
            storage.record_published(post_record)
            recent_titles.append(article.title)
            logger.info(f"Successfully posted! [{posts_published_in_run}/{settings.MAX_POSTS_PER_RUN} in current run]")
        else:
            daily_stats.publish_failed += 1
            storage.record_failure(article.content_hash, pub_error or "Unknown Facebook publishing failure")

    # 8. Persist Updated Daily Stats
    storage.update_daily_stats(daily_stats)

    # 9. Daily Summary & Capacity Report
    logger.info("=" * 60)
    logger.info("CYCLE EXECUTION SUMMARY:")
    logger.info(f"Date (Cairo): {today_cairo}")
    logger.info(f"Articles Discovered: {daily_stats.articles_discovered}")
    logger.info(f"Duplicates Skipped:  {daily_stats.duplicates_skipped}")
    logger.info(f"Articles Rejected:   {daily_stats.articles_rejected}")
    logger.info(f"Captions Generated:  {daily_stats.captions_generated}")
    logger.info(f"Published Today:     {daily_stats.posts_published} / {settings.DAILY_LIMIT}")
    logger.info(f"Remaining Capacity:  {max(0, settings.DAILY_LIMIT - daily_stats.posts_published)}")
    logger.info(f"Tokens Consumed:     ~{daily_stats.estimated_llm_tokens}")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_pipeline()
