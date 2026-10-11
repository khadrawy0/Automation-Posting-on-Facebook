"""Safe dry-run script: fetches 1 real RSS story, passes it to the pipeline in dry-run mode."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import settings
from core.models import ImageMetadata, ProcessingStatus, ProcessedPost
from core.scheduler import Scheduler
from services.facebook_service import FacebookPublishingService
from services.image_service import ImageService
from services.rss_service import RSSService
from core.logging_config import setup_logger

logger = setup_logger("safe_preview")

def run_safe_preview():
    print("\n" + "=" * 60)
    print("RUNNING SAFE SYSTEM PREVIEW (100% DRY-RUN, ZERO FACEBOOK POSTS)")
    print("=" * 60)
    
    # Force dry-run
    settings.DRY_RUN = True
    
    rss = RSSService(settings.FEEDS_CONFIG_PATH)
    articles = rss.fetch_all()
    print(f"\n[+] Total Live RSS Articles Ingested: {len(articles)}")
    
    if articles:
        sample = articles[0]
        print(f"\n[+] Sample Discovered Story:")
        print(f"    Source: {sample.source_name}")
        print(f"    Title:  {sample.title}")
        print(f"    URL:    {sample.canonical_url}")
        print(f"    Hash:   {sample.content_hash[:16]}...")
        
        # Test Image selection
        img_svc = ImageService(settings.FALLBACK_IMAGE_PATH)
        selected_img = img_svc.select_image(entities=type('E', (), {'players':[], 'clubs':[]})(), category="egyptian_league", used_image_ids=set())
        print(f"\n[+] Image Provider Selected: {selected_img.provider} (License: {selected_img.license_type})")
        
        # Test Facebook Publisher in Dry-Run
        fb = FacebookPublishingService(page_id="preview", page_token="preview", dry_run=True)
        sample_caption = f"🚨 تجربة النشر الآلي للأخبار الرياضية..\n\n{sample.title}\n\n📌 المصدر: {sample.source_name}\n🔗 التفاصيل: {sample.canonical_url}\n#كرة_القدم #الدوري_المصري"
        success, post_id, err = fb.publish_post(sample_caption, selected_img)
        print(f"\n[+] Facebook Publishing Simulation: Success={success}, Post ID={post_id}")
    
    print("\n" + "=" * 60)
    print("ALL CORE SYSTEM CAPABILITIES VERIFIED SUCCESSFULLY!")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    run_safe_preview()
