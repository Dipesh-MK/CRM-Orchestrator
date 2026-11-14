import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 950})

        print("Navigating to http://localhost:8080/...")
        await page.goto("http://localhost:8080/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)

        # Send compliant dental campaign prompt to reach card review
        chat_input = page.locator("#chatInput")
        send_btn = page.locator("#sendBtn")
        
        dental_prompt = (
            "We are Apex Dental Clinic. We want to send a friendly reminder to our existing patients "
            "for their annual dental cleaning and checkup for $89 (regularly $160). Valid through November 30th. "
            "Patients can book appointments online at https://apexdentalclinic.com/clean."
        )
        await chat_input.fill(dental_prompt)
        await send_btn.click()

        print("Waiting for campaign form to appear...")
        await page.wait_for_selector("[id^='cf-step1-']", timeout=60000)
        await page.wait_for_timeout(1000)

        # Switch to Step 2 (Tone Selection & Message Preview)
        print("Switching to Step 2 (Tone Selection)...")
        await page.evaluate("() => { const fId = Object.keys(window.activeCampaignForms)[0]; goToFormStep(fId, 2); }")
        await page.wait_for_selector("[id^='cf-step2-']:not(.hidden)", timeout=10000)

        # Locate tone feedback input and regen button
        feedback_input = page.locator("[id^='cf-tone-feedback-']").first
        regen_btn = page.locator("[id^='cf-tone-regen-btn-']").first
        
        print("Typing 'very very aggresive' into tone customization input...")
        await feedback_input.fill("very very aggresive")
        await regen_btn.click()

        print("Waiting for Guardrail Interception banner...")
        # Check that the refusal banner renders
        refusal_banner = page.locator("text=Guardrail Interception: Tone Request Refused")
        await refusal_banner.wait_for(timeout=15000)

        status_text = await page.locator("[id^='cf-tone-regen-status-']").first.inner_text()
        print("UI Refusal Banner Content:\n", status_text)
        
        assert "guardrail interception" in status_text.lower(), "Expected Guardrail Interception banner"
        assert "prohibited tone register detected" in status_text.lower(), "Expected prohibited tone reason"
        assert "compliant alternatives" in status_text.lower(), "Expected compliant alternatives"

        await refusal_banner.scroll_into_view_if_needed()
        await page.wait_for_timeout(500)

        # Take verified snapshot
        os.makedirs("snapshots", exist_ok=True)
        screenshot_path = "snapshots/ui_tone_refusal_verified.png"
        await page.screenshot(path=screenshot_path)
        print(f"Saved verified screenshot to {screenshot_path}")

        await browser.close()
        print("UI TONE REFUSAL VERIFICATION PASSED!")

if __name__ == "__main__":
    asyncio.run(run())
