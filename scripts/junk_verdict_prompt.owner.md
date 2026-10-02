You label short chat messages (10-39 characters) for the owner's personal search index. A JUNK row is hidden from search; a KEEP row stays findable. The owner's bar is strict: in a random sample of this band he marked about 9 in 10 rows JUNK.

Decide for the TARGET line only (prev/next are context, and they stay searchable whatever you decide):

  KEEP  - only if the TARGET itself carries something the owner would plausibly search for later: a concrete plan or arrangement with its specifics ("Så henter jeg deg der 23", "Jeg kan 25. og 26."), a decision or status ("Da er vaktene dekket", "Sykdom på bup, IKKE DRA"), a real fact or problem ("End date satt til tidligere enn start."), money ("100 til hver av dere"), a file or a link to real content ("sittekart anbefaling.docx", "https://helltides.com/pit"), a handover of something ("her har du historikken siste 6 mnd"), or a full name standing alone ("Pål Even Jahren").
  JUNK  - everything else, including much that looks informative at first glance: acknowledgements, agreement, greetings, affection, reactions and emotion, jokes and banter, small talk, questions without a lasting answer, passing logistics ("Flott 🥰 da er eta 15:10?", "Tar bare 5 min", "på vei"), a first name plus a request ("Ole Kristian kan du fikse ?"), tracking or throwaway links, food and errands, and replies whose content lives only in the neighbouring message.

Owner-labelled examples:
  KEEP: "VFØR eidene. Si ja?" | "Er RX11 skien byttet til R11 Skien?" | "Stiller! Møter direkte ca. 35 minutter." | "Tar med tre skjermer når jeg stikker" | "Kan du plis ta med opp til meg 🥺" (after "Har du en ladekloss?") | "Endringsledelse anbefales!"
  JUNK: "du er ikke på kontoret i dag ?" | "Hvordan unsubber jeg mappen?" | "Den var da av type high ?" | "Christopher med" | "Buffalo wild wings" | "reddot winner 2005" | "House tour" | "Jeg kanke ta ut fra den" | "E det greit med bare screenshot?" | "ja det vil jeg gjerne" | "Oi, grattis" | "det er sikkert ok" | "Dette er helt krise"

When unsure, JUNK.

Output exactly one line per message: "<n>: JUNK" or "<n>: KEEP". Nothing else.
