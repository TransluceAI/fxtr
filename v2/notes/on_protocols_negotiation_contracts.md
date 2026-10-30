Author: [Daniel Johnson](mailto:daniel@transluce.org)
Originally located at: https://docs.google.com/document/d/1DSRDSPFNO7-AZVG0PqvjAfZJPciiDgzjIaMrBbGGx8o/edit?tab=t.0#heading=h.nmtjlyiqz0sd

This document has some of my thoughts on protocols, schemas, and API contracts, and is an attempt to communicate how I’m currently thinking about fxtr.

It has roughly two parts: first, I give some high level thoughts and describe where I am imagining fxtr could go, second, I reply to some specific comments from Rob in \#behaviors-eng and try to connect them with those thoughts.

## Some goals of fxtr

A possibly useful way of thinking about fxtr’s design is in terms of a few primary goals:

* **Cross-compatibility / Interoperability:**
  * Both for our own internal operations, and for the AI ecosystem as a whole, it is very useful if research outputs are compatible with each other and can be combined and re-used
  * \=\> Design goal: make it easy to express primitives that naturally interoperate with each other. Ideally, fxtr would be set up so that the default, most convenient way an individual programmer would want to implement something is *also* well-set-up for other people to re-use it.
* **Stability / Version Control:**
  * Being able to stably refer to a specific version of an object is important for both
    * Scientific rigor and systematic evaluation
    * Parallel co-development of features and ideas
  * The scientific / systematic evaluation use case also requires all versions of the object to be *simultaneously available*, which is not always needed when iteratively developing improved versions of something
  * \=\> Design goal: Make it possible to stably refer to specific versions of runnable code and procedures (like “a judge”), each of which might use different versions of the codebase, without them interfering with each other or requiring significant manual effort.
* **Inspectability / Observability / Provenance:**
  * Being able to inspect results and trace back how they were computed creates natural incentives for those results to be valid (by enabling that validity to be audited / verified by interested parties)
    * As automated systems do increasing amounts of work, it is going to be difficult to keep track of everything they do, which correspondingly may reduce their validity by default
  * Additionally, exposing the traces of how data was produced in a standard form makes it easier to automate the auditing/verification process (e.g. via an agent)
  * \=\> Design goal: Make it possible to trace any computation result back through the process that produced it, and make it easy to observe the state of the data at various stages of the process.
* **Completeness / Closedness under Composition**
  * As we build better primitives for individual tasks, we will want to combine those primitives into larger “workflows”, that can then themselves be thought of as individual tasks and composed
  * \=\> Design goal: Allow this composition to be itself expressed within fxtr, so that we can still satisfy the above design goals, at least for the types of composition that we will do at Transluce toward understanding AI systems or publishing evaluations of them
* **Ease of Use**
  * Fxtr should be a tool that speeds us up internally, not one that slows us down
  * It should also be easy enough to use, relative to the value it provides, that others adopt it
  * \=\> Design goal: Fxtr should help researchers and speed them up rather than getting in your way

These goals motivate many of the aspects of fxtr’s design.

## What fxtr is trying to be

Fxtr does a lot of different things, but I think those things are complementary. In short, fxtr is trying to:

* *\[primary, most important\]* Define (and try to standardize) formats and conventions for:
  * **Describing input data, output data, and configuration data (and its relationship to code)**
    * Guaranteeing that the version of that data that you have is stable/immutable (=\> version control)
    * Linking those pieces of data to all other pieces of data that contributed to it (=\> provenance tracking)
    * Identifying the *meaning* of that data, as intended by the programmer (=\> observability, safe interoperability)
  * **Transforming immutable configuration data into a runnable service deterministically / hermetically**
    * So that you can describe a computational process as data (=\> provenance, stability, version-control)
    * For instance, given “the config of a judge”, plus minimal per-user state like API keys, you should be able to instantiate and query the judge, and it should be unambiguous what needs to happen for this to work
    * This implies that the configuration data must contain all information needed to unambiguously reference a specific version of the code needed to faithfully instantiate it. (So, closer to a Dockerfile than to an experiment\_config.yaml)
  * **Describing computation graphs of tasks which combine those services, and their results, as immutable data**
    * So that you can describe and trace through a full scientific experiment, or other fixed methodology (=\> provenance, completeness)
  * **Allowing those results to be inspected both visually by humans and over an API by agents, without needing external context**
    * So that you can point someone (or an agent) to a specific piece of data and figure out what it means and whether it is doing what one would expect (=\> inspectability, observability, provenance)
* *\[needed for the above to actually work or be valuable\]* Build tooling and software to make it easy to
  * Build scalable experiment pipelines that use the above formats
  * Iteratively adopt pieces of the conventions (tending toward full adoption)
  * Express both early research and late-stage “production” workflows in fxtr
  * Share results with each other and the community
  * Visualize and trace through data, experiments, results
  * Make it easy enough to use that fxtr becomes the “default” way to write new experiment code, rather than being a painful / laborious process

This is a pretty large set of things to support. Each of these activities is something that contributes to the overall goals, but few of the activities exactly match the way most ordinary software is written today\! This leads to the fundamental tension that has persisted through most of fxtr’s development:

* writing code that achieves these goals requires\* writing it differently,
* but, writing code differently has costs, especially without an existing ecosystem to build on

*(\*At least, I think it basically requires this at ecosystem scale. An individual person can achieve these goals with discipline but a community or a swarm of AI agents will not have this luxury.)*

The ideal I am currently striving for is something like,

* (a) it should be *possible* to express code that is minimally adapted from its original form to be compatible with fxtr,
* (b) it should be *easy* to adopt more of the conventions of fxtr from that point
* (c) and each such convention you adopt should *provide immediate value* to you.

It is far from this, currently, but I hope it is getting closer.

## Layers of Interoperability / Compatibility

There is a conceptual framework I have been thinking about recently, partially based on (and in response to) some of the good feedback from Rob, Cassidy, and Neil, which I think might be useful for thinking about how our software should interoperate (and then, more specifically, what fxtr should do to enable this). Roughly, I think we can break up “interoperability” in many systems as consisting of a few different layers:

1. The **routing layer:** fundamentally, over what routes do the different computations communicate with each other? What affordances do they have for connecting to each other and exchanging data, what determines whether they can connect and what they can actually send?
   * HTTP is an example of this. Clients can identify servers based on a URL, and then make a connection to them using this. Servers can also connect to each other. This produces a request/response exchange of data, each of which consist of a response code, headers and a payload.
   * A websocket provides a different type of connection where each side initially connects via a URL but then begins a persistent bidirectional exchange of packets.
   * IPLD and IPFS are an interestingly different routing layer: they route information based on content-based hashes, which uniquely address the data independent of what machine it is actually running on.
2. The **negotiation layer:** once two computations are connected over some route, how do they “agree” on what is supposed to happen over this route; how do they determine what needs to be done?
   * An example of this is how, for an HTTP request, a client can request a specific MIME type, e.g. “text/html” or “text/markdown; charset=utf-8” or “application/json”, and the server can choose to respond in the requested format. Some websites use this to serve LLM-compatible sites to LLM clients, while serving rich HTML to browsers.
   * Another example is how ssh clients decide which private key to sign with, and how ssh servers or git servers match this to a user.
   * Another common example involves switching or “upgrading” protocols: e.g. every websocket connection starts as an HTTP connection with a specific set of headers, which indicate a request to transform the underlying TCP connection into a websocket connection and stop speaking HTTP.
   * Although possibly a stretch, we can think of the URL of a HTTP request as a negotiation step that is used by a server to figure out what resource it is supposed to send vs what resources the server actually has.
3. The **schema / encoding layer:** how is the application data of the computation formatted? What is necessary to parse the request and send a correctly formed response?
   * Many APIs speak JSON. This is a kind of encoding, and can also sort of be seen as a very broad schema. It defines some requirements of what a valid input is allowed to be.
   * Many APIs also document the JSON schemas of their endpoints. This describes the expected *shape* of the JSON that they expect a caller to provide, and the expected shape of the response.
   * There are also non-JSON schemas. For instance, some unix command-line programs use tab characters to separate tables, and other commands accept any such tab-delimited table and can process the data in it easily.
   * Some applications, especially internal ones, speak more complex schemas defined by e.g. protocol buffers (protobufs).
   * Note that, by this layer, I do not mean to include the actual *enforcement mechanism* or *schema language*. Rather, I mean the implicit schema or encoding that the application actually uses, and which determines whether it can actually handle the request or response correctly. An endpoint might have a very specific JSON schema that it accepts, even if nobody ever actually documents this schema as a JSON schema declaration.
4. The **error detection layer:** how can we tell whether the computation that was requested or was performed is the correct one, and that both parties agree on what was supposed to happen? What do we do if one party fails to uphold the (explicit or implied) contract about the behavior of the other party?
   * This is a continuum, which includes approaches like:
     * Detect incorrect expectations at the negotiation stage: Clearly describe to the other party what expectations you have, and then error out early if the other party declares those expectations cannot be met. (This happens if you try to e.g. push to a git repo you don’t have write permissions to; the push data doesn’t actually get sent to the server.)
     * Detect expectations that were not met at the schema level: Compare the format of the data you receive from the other party to the format you expect to receive, and error out if you get data that is malformed. (This is what happens if you e.g. use Pydantic to parse the inputs to your web server.)
     * Detect application-specific logic issues: Parse the data out, then apply some logic “outside of the protocol” to figure out if it “makes sense” (e.g. does this signature actually match the expected public key? does the requested user ID exist in the database?)
     * Let it fail: Simply assume all data you get is correct, and do *something* based on it, without specifically trying to catch errors. (This can either be a problem for *you*, if your code depended on a contract being met and the other party didn’t meet it, or simply a problem for *the other party*, if it turns out they weren’t meeting *your* implied schema contract and they get garbage behavior from you as a result.)
   * Also, fundamentally, what counts as an “error” is very application specific and use-case specific.
     * As one example, consider the various inconsistencies between log prob computations you get when running with VLLM in different configurations, e.g. between sampling and scoring. Is this an “error”? Well, it depends on the implied contract between you and VLLM. VLLM is following the *schema* of an OpenAI responses server even when it produces results that aren’t what you expect.
     * As another example, suppose there is a function that takes in info about a person and saves this info to the database, and suppose the user passes both a “name” field and an “age” field but the server doesn’t know about the “age” field. Is it acceptable for the server to silently ignore “age” and only store “name”? Well, this depends on what the user’s expectations were, probably, and whether that matches the server’s behavior, and if not then which expectation was “the correct contract” between them.
       * If I had to choose, I would prefer that the server either *reject* this data (because it is malformed and possibly indicates user error), or *explicitly document* its contract as being that it only stores the name and not any other fields passed (in which case it is clear that it is the *user* who is responsible for losing their own data).
       * Of course there are other cases where this might not be necessary, e.g. if there is an internal closed-world codebase where everyone knows the set of objects flowing through the system, and we know that one part of the system doesn’t care at all about the age or that it can retrieve the age later from some external source of truth, it may very well be fine to ignore the age field here (although, in this case, it seems odd for the user to send it)
5. The **library layer:** how do developers actually express the behavior they want, such that the computation ends up doing the right thing across the other layers?
   * Many libraries exist to essentially standardize or automate the above layers. E.g. pydantic is good for schema validation, and can be used both to define the schemas and to do error detection for them. FastAPI helps negotiate between incoming HTTP requests and python functions you want to run in response, and includes middleware for negotiating some of these connections up to websocket connections.
   * Ultimately whether something is “easy” depends a lot on either (a) how much library-layer support it has, or (b) how simple it is to operate without library-layer support.
   * Libraries can also make use of the structure in the above layers to optimize behavior by using knowledge about the developer’s intent. For instance, many libraries can automatically handle caching for you, based on well-known HTTP headers that essentially operate at the negotiation layer, and this seamlessly works with CDNs too if you use them.

## How fxtr fits into these layers (in the past and in the future)

### Past: Fxtr v1 (DataclassEntityBase / FxtrCallTarget, currently in use)

The first version of fxtr was built with a pretty clear intuitive sense of the goals and the “shape” of what the final code that achieved those goals would look like, but without a very fleshed-out sense of the design space or migration path, and without explicitly thinking about the layers of interoperability from above. Because of this, in the early fxtr design I heavily focused on:

* the **error detection** layer (how do we make sure we can detect any violation of expectations),
* the **Python library** layer (what, specifically, do immutable content-addressed data types look like when implemented in Python),
* a *fairly restricted subset of Python code / programming conventions* that I could support comfortably with these two layers

I then tried to somewhat haphazardly expand the subset of code it supported (e.g. by adding type variables), but without fully thinking through how they would interoperate with everything else to achieve the goals of the project.

This was informative for me about the design space, but I think it was *not* a good choice from the perspective of onboarding people onto the library and having it speed up their research. I am very grateful for the feedback I have received from early users, and I’m sorry for all the rough edges that I haven’t been able to support yet.

The biggest learning I have taken from fxtr v1 is that the Python library and error detection layers are NOT the best places to design a set of conventions like this, and that it is much more important to nail the routing and negotiation layers. In particular:

* If someone has both (a) established a (stable, version-controlled) connection to the other party (or data source) that they want to connect to, and (b) determined what “contract” governs this connection and what the expectations are on both sides for “correct” behavior, the hard part is done. Enforcing additional restrictions on code that uses the library, in the name of error checking, schema definition, and consistent behavior, is sort of unnecessary, because it is already the application’s job to uphold its own contract; the library is just there to help.
* Given this, the correct place to think about the primitives fxtr provides should not be at the Python class level, but instead at a much lower level, at the routing and negotiation boundaries of the system. The focus should be “how do we *allow* developers to communicate the contracts on the data and functions they define in a content addressed way, if they want to do that, to enable them to express their intent clearly and enable traceability" rather than “how do we design a Python DSL where every developer is *required* to communicate those contracts in a content addressed way”

### Present / Future: “fxtr2” (FxtrScope, FxtrID, FxtrNamespace, more to come)

My current work on “fxtr2” (the second iteration of fxtr, technically a separate python package in the fxtr repo) has tried to take a bottom-up approach: my goal is to provide very stable lower-level primitives that simplify expressing data in the way fxtr expects.

So far I have been working on something like the “routing” layer, with bits of the “negotiation” layer, although not exactly because currently the only thing I have redesigned is the data format. The key principles of this piece are essentially:

* Fxtr defines a contract for a type of data, called an “entity”. To qualify as an “entity”, the object must be a JSON-like dict, that
  * Can include arbitrary JSON-serializable types composed arbitrarily
  * Should use a specific field (“$fxtr.type”) as a type discriminator, where this field should be a “FxtrID” that points to the “contract” that objects of this type uphold
    * (but where the exact type of that “contract” is up to the programmer, and I don’t know what the idiomatic way to set this up should be yet. the simplest way is to have the FxtrID refer to e.g. some domain name you own along with a name for the type, and then the contract is whatever you say it is and we trust you to not change the contract. I think we can do better but still figuring it out)
  * Can also include other “fxtr IDs” which refer to other entities
  * (technically, must also include a specific marker (field “$fxtr.ctx”) that has no content, which is going to be used in the future as a place to attach provenance tracking metadata)
* If you give fxtr a valid entity to store, it will store it and give you a FxtrID that resolves to that entity.
* If you try to store a reference to some other FxtrID as part of your entity, you must have retrieved that FxtrID from fxtr or validated it with fxtr previously, so that it knows where it is.
* If fxtr gives you a FxtrID, you are allowed to load that FxtrID to retrieve the data it contains. (The things that count as “giving you a FxtrID” include getting an ID from storing an entity, looking up an ID from storage, or receiving a FxtrID by loading an entity that contains them)
* (And then there’s an opt-in convenience layer on top that tries to help you serialize and deserialize things more easily via type annotations.)

Basically, the application is allowed to treat FxtrID as equivalent to the transitive closure of its entity dict (in that having a valid FxtrID is equivalent to having the data for that ID and everything it refers to), and fxtr handles the “routing” to let you load that data and trace back to the data it references.

Additionally, the “$fxtr.type” convention serves the “negotiation” role, by allowing you to confirm that the type you got is one of the types you know how to deal with. This doesn’t have to be done based on a fixed closed set of discriminators, but could be potentially done by even *loading* the contract and checking if it has some property, e.g. the “$fxtr.type” of some object could itself be the ID of some type-contract object like
{
  “implements”: \[ \<hash ID of FooProtocol\>, \<hash ID of FooProtocolWithAnotherField\> \],
  “$fxtr.type”: \!builtin.data\_subtype\_contract,  \# \<- the type of the contract itself, which is a builtin (perhaps?)
}
which would mean that a caller is free to assume that the data fits the contract of FooProtocol and also that it fits the contract of FooProtocolWithAnotherField, whichever it needs (but where, presumably, FooProtocol says “other fields may be present and don’t have a defined meaning”).

But, for now, fxtr doesn’t actually enforce what the negotiation protocol *is* or what the contract should be, it just provides a canonical place for you to store and retrieve that contract. It is assumed that cooperating parties can bootstrap up to a consistent (implicit or explicit) contract of what data is supposed to represent as long as there is a mechanism for which they can easily encode/decode their expectations.

The next big things I want to figure out are:

* What the equivalent of this “routing/negotiation-first” approach is for call targets / services (probably the service itself should be allowed to respond to arbitrary JSON-RPC or similar, but should have a similar “$fxtr.type” for negotiation)
* How to actually implement this “data\_subtype\_contract” idea idiomatically at the library level, to make it easier to check these contracts (without restricting the ability to define newer more exotic ones) (I think Rust traits are a good idea and want to see if we can build “Rust for JSON” out of this)
* What other conventions are worth encoding in the library level as convenience features, to make this system feel like it saves time overall

I think, regardless, the system will NOT require encoding the contract as some particular set of Python types. It would be *nice* if there was some Pydantic-like way of converting from Python type definitions to a contract type, but I think perhaps this is not really worth spending much time on, and instead it will be the responsibility of the programmer to encode their types in a way that both satisfies the Python typechecker and also correctly satisfies the contract they are declaring.
