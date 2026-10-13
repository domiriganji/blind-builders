---
title: Putting your web app on Android and iPhone home screens, and in app stores
summary: Make a web app that people can add to their home screen with no developer account, then the general route into Google Play and the App Store, with every step that needs a paid developer account marked.
last_checked: 2026-10
order: 5
---

## The short version

- A web app can sit on an Android or iPhone home screen like any other app. This needs **no developer account, no fee and no review**.
- To make it installable, the website needs a small description file called a web app manifest, and it must be served over https.
- Getting into **Google Play** usually means wrapping the web app in a Trusted Web Activity. This **needs a Google Play developer account**.
- Getting into **Apple's App Store** **needs an Apple Developer Program membership** and a Mac with Xcode, and Apple may reject an app that is only a website in a wrapper.
- Whatever route you take, test with TalkBack and VoiceOver, and ask for a [peer check](/checks).

## Part 1: home screen apps (no developer account needed)

### What your website needs

Ask the AI in [Build](/build) to "make this an installable web app". These are the parts it should add:

1. **A web app manifest.** A small file, often called `manifest.webmanifest` or `manifest.json`, linked from the head of every page like this:

   ```
   <link rel="manifest"
         href="/manifest.webmanifest">
   ```

   It gives the app's name, a short name for under the icon, the page it opens on (`start_url`), `"display": "standalone"` so it opens without the browser's address bar, and icons in a few sizes.
2. **https.** Browsers only install web apps served over a secure connection.
3. **A service worker, if you want it to work offline.** This is a script that keeps copies of your pages so the app opens without a connection.

Give the app a short name that makes sense when a screen reader reads it on the home screen, because that is how people will find it.

### Adding it on Android with TalkBack

1. Open the website in Chrome.
2. Find the "More options" button at the top of the screen and double-tap it.
3. Choose "Install app" or "Add to home screen". The wording depends on the Chrome version and on whether the site has a manifest.
4. Confirm. The app's icon appears on the home screen, with the short name from the manifest.

### Adding it on an iPhone with VoiceOver

1. Open the website in Safari.
2. Find the Share button. On some iOS versions it is inside a "More" button.
3. Choose "Add to Home Screen", then "Add".

Once a web app is on the home screen, it opens in its own window. You can update it any time by changing the website: there is nothing to resubmit.

## Part 2: Google Play (needs a Google Play developer account)

The usual route for a web app is a **Trusted Web Activity**: a small Android app that opens your website full screen using Chrome. Your website stays the real app, so you keep updating it in one place.

1. **Make the web app installable first** (Part 1). Store packaging tools read your manifest.
2. **Create the Android package.** Two free tools do this:
   - **Bubblewrap**, a command-line tool from Google's Chrome team. With Node.js installed, run these three commands one at a time, using your own site's manifest address, and answer the questions the second one asks ([Bubblewrap on GitHub](https://github.com/GoogleChromeLabs/bubblewrap)):

     ```
     npm i -g @bubblewrap/cli
     bubblewrap init --manifest=https://your-site.example/manifest.webmanifest
     bubblewrap build
     ```

     A command-line tool can suit a terminal and screen reader well. It may offer to download the Java and Android tools it needs.
   - **PWABuilder**, a website from Microsoft that packages a web app for Google Play and other stores ([pwabuilder.com](https://www.pwabuilder.com)). We have not checked how well its pages work with screen readers.
3. **Prove you own the website.** Put a file at `/.well-known/assetlinks.json` on your site with your app's package name and its signing key fingerprint. Bubblewrap and PWABuilder tell you what to put in it. Without it, Android shows a browser address bar at the top of your app.
4. **Create a Google Play developer account** (needs an account and a registration fee). Before you start, read the current fee, identity checks and any testing rules for new accounts in [Play Console Help](https://support.google.com/googleplay/android-developer/). They change, and the research behind this guide did not check them.
5. **Upload the app bundle** that Bubblewrap or PWABuilder made (a `.aab` file), fill in the store listing, and send it for review (needs the developer account).

Keep the signing key file and its password somewhere safe and private. You need them for every update, and you should never share them with anyone, including the AI.

## Part 3: Apple's App Store (needs an Apple Developer Program membership)

- **You need an Apple Developer Program membership**, which has a yearly fee, and a Mac with Xcode to build and upload the app. Check the current terms on [Apple's developer site](https://developer.apple.com/programs/).
- Apple's App Store Review Guidelines expect an app to be more than a repackaged website (the "minimum functionality" rule, guideline 4.2). A wrapped web app with nothing else may be rejected ([App Store Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)).
- Tools such as Capacitor wrap a web app in a native iOS and Android project, so you can add features that need the phone, such as notifications or the camera ([Capacitor](https://capacitorjs.com)).
- In App Store Connect, you can declare which accessibility features your app supports, such as VoiceOver, in its Accessibility Nutrition Labels. They show on your app's store page ([Apple: overview of Accessibility Nutrition Labels](https://developer.apple.com/help/app-store-connect/manage-app-accessibility/overview-of-accessibility-nutrition-labels)). Only declare what someone has actually tested.

For many projects, a home screen web app (Part 1) is enough, and it avoids the fee and the review.

## What we do not know yet

The research behind this site found no evidence from April to October 2026 that the accessibility of App Store Connect or the Google Play Console improved, and we have not tested them with screen readers ourselves. If you have published through either one with a screen reader, please share what worked: [ask a question about this guide](/help/ask?title=Putting%20your%20web%20app%20on%20Android%20and%20iPhone%20home%20screens%2C%20and%20in%20app%20stores) and add what you found.

## Before you publish

- Test the installed app with TalkBack on Android and VoiceOver on iPhone: headings, button names, form labels, and where focus goes after each action.
- Ask blind and low-vision members for a [peer check](/checks) on the phones they use.
- Make sure the app's name and icon label read well, because that is the first thing people hear or read.
