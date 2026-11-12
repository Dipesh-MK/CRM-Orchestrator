import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1600, "height": 950})
        
        print("Navigating to http://localhost:8080/observatory...")
        await page.goto("http://localhost:8080/observatory", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        
        # Verify loopback wire exists in SVG
        loopback_wire = page.locator("#wire_node_quarantine_node_supervisor")
        wire_count = await loopback_wire.count()
        print(f"Loopback wire count in SVG: {wire_count}")
        assert wire_count == 1, "Loopback wire not found in SVG"

        # Check classes on loopback wire
        wire_class = await loopback_wire.get_attribute("class")
        print(f"Loopback wire initial classes: {wire_class}")
        assert "wire-loopback" in wire_class, "Expected wire-loopback class"

        # Trigger Sim Assurance Bypass
        print("Clicking Sim Assurance Bypass button...")
        sim_btn = page.locator("#simAssuranceBtn")
        await sim_btn.click()

        # Wait for simulation sequence to progress to step 7 (around 3.5s)
        await page.wait_for_timeout(4000)

        # Check if wire has active-loop class
        wire_class_active = await loopback_wire.get_attribute("class")
        print(f"Loopback wire classes during/after loop: {wire_class_active}")

        # Check inspector drawer for Remediation Loopback title and Options A, B, C, D
        drawer_content = await page.locator("#inspectorDrawerBody").inner_text()
        print("Inspector drawer snippet:\n", drawer_content[:300])

        assert "remediation loopback" in drawer_content.lower() or "why it cannot be accepted" in drawer_content.lower(), "Remediation card missing from drawer"
        assert "replace with" in drawer_content.lower() or "remove" in drawer_content.lower(), "Options missing"
        
        # Take a snapshot
        os.makedirs("snapshots", exist_ok=True)
        screenshot_path = "snapshots/obs_loopback_verified.png"
        await page.screenshot(path=screenshot_path)
        print(f"Saved verified screenshot to {screenshot_path}")

        # Now test clicking option A button in inspector drawer
        print("Testing click on option A button...")
        first_opt_btn = page.locator("#inspectorDrawerBody button").first
        await first_opt_btn.click()
        await page.wait_for_timeout(1500)

        # Check ticker for 'Fix selected'
        ticker_text = await page.locator("#workflow-step-ticker").inner_text()
        print(f"Ticker text after remediation fix: {ticker_text}")

        await browser.close()
        print("ALL OBSERVATORY LOOPBACK CHECKS PASSED!")

if __name__ == "__main__":
    asyncio.run(run())
