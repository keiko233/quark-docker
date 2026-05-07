import { chromium } from "npm:playwright";

const QUARK_PAN_CLIENT_OFFICIAL_SITE_URL = "https://pan.quark.cn/";

async function main() {
  const installerPath = `${Deno.cwd()}/installer.exe`;

  const browser = await chromium.launch({
    headless: true,
  });
  const context = await browser.newContext({
    acceptDownloads: true,
  });
  const page = await context.newPage();

  try {
    await page.goto(QUARK_PAN_CLIENT_OFFICIAL_SITE_URL, {
      waitUntil: "domcontentloaded",
    });

    const downloadButton = page.locator(
      'div.download-btn:has-text("下载客户端")',
    ).first();
    const windowsDownloadCard = page.locator(".download-item.win.is-click")
      .first();

    await downloadButton.waitFor({ state: "visible" });
    await downloadButton.click();
    await windowsDownloadCard.waitFor({ state: "visible" });

    const installerRequestPromise = page.waitForRequest((request) => {
      return /https:\/\/umcdn\.quark\.cn\/download\/.*\.exe/i.test(
        request.url(),
      );
    });

    await windowsDownloadCard.click();
    const installerRequest = await installerRequestPromise;
    const installerResponse = await fetch(installerRequest.url());

    if (!installerResponse.ok || !installerResponse.body) {
      throw new Error(
        `Download failed: ${installerResponse.status} ${installerResponse.statusText}`,
      );
    }

    const installerFile = await Deno.open(installerPath, {
      create: true,
      truncate: true,
      write: true,
    });
    await installerResponse.body.pipeTo(installerFile.writable);

    console.log(`Installer saved to ${installerPath}`);
  } finally {
    await context.close();
    await browser.close();
  }
}

if (import.meta.main) {
  await main();
}
